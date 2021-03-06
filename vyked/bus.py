import asyncio
from collections import defaultdict
from functools import partial
import json
import logging
import random
import uuid

from again.utils import unique_hex

from retrial.retrial import retry
import aiohttp

from .services import TCPServiceClient, HTTPServiceClient
from .pubsub import PubSub
from .packet import ControlPacket, MessagePacket
from .protocol_factory import get_vyked_protocol
from .utils.jsonencoder import VykedEncoder

HTTP = 'http'
TCP = 'tcp'

_logger = logging.getLogger(__name__)


def _retry_for_client_conn(result):
    if isinstance(result, tuple):
        return not isinstance(result[0], asyncio.transports.Transport) or not isinstance(result[1], asyncio.Protocol)
    return True


def _retry_for_pub(result):
    return not result


def _retry_for_exception(_):
    return True


class HTTPBus:
    def __init__(self, registry_client):
        self._registry_client = registry_client

    def send_http_request(self, app: str, service: str, version: str, method: str, entity: str, params: dict):
        """
        A convenience method that allows you to send a well formatted http request to another service
        """
        host, port, node_id, service_type = self._registry_client.resolve(service, version, entity, HTTP)

        url = 'http://{}:{}{}'.format(host, port, params.pop('path'))

        http_keys = ['data', 'headers', 'cookies', 'auth', 'allow_redirects', 'compress', 'chunked']
        kwargs = {k: params[k] for k in http_keys if k in params}

        query_params = params.pop('params', {})

        if app is not None:
            query_params['app'] = app

        query_params['version'] = version
        query_params['service'] = service

        response = yield from aiohttp.request(method, url, params=query_params, **kwargs)
        return response


class TCPBus:
    def __init__(self, registry_client):
        self._registry_client = registry_client
        self._client_protocols = {}
        self._pingers = {}
        self._node_clients = {}
        self._service_clients = []
        self._pending_requests = []
        self.tcp_host = None
        self.http_host = None
        self._host_id = unique_hex()
        self._ronin = False
        self._registered = False

    def _create_service_clients(self):
        futures = []
        for sc in self._service_clients:
            for host, port, node_id, service_type in self._registry_client.get_all_addresses(sc.properties):
                if service_type == 'tcp':
                    self._node_clients[node_id] = sc
                    future = self._connect_to_client(host, node_id, port, service_type, sc)
                    futures.append(future)
        return asyncio.gather(*futures, return_exceptions=False)

    def register(self, host, port, service, version, clients, service_type):
        for client in clients:
            if isinstance(client, (TCPServiceClient, HTTPServiceClient)):
                client.bus = self
        self._service_clients = clients
        self._registry_client.register(host, port, service, version, clients, service_type)

    def registration_complete(self):
        if not self._registered:
            f = self._create_service_clients()
            self._registered = True

            def fun(_):
                if self.tcp_host:
                    self._clear_request_queue()

            f.add_done_callback(fun)

    def send(self, packet: dict):
        packet['from'] = self._host_id
        func = getattr(self, '_' + packet['type'] + '_sender')
        func(packet)

    def _request_sender(self, packet: dict):
        """
        Sends a request to a server from a ServiceClient
        auto dispatch method called from self.send()
        """
        self._pending_requests.append(packet)
        self._clear_request_queue()

    @retry(should_retry_for_result=_retry_for_client_conn, should_retry_for_exception=_retry_for_exception, timeout=10,
           strategy=[0, 2, 2, 4])
    def _connect_to_client(self, host, node_id, port, service_type, service_client):
        future = asyncio.async(
            asyncio.get_event_loop().create_connection(partial(get_vyked_protocol, service_client), host, port))
        future.add_done_callback(
            partial(self._service_client_connection_callback, self._node_clients[node_id], node_id, service_type))
        return future

    def _service_client_connection_callback(self, sc, node_id, service_type, future):
        _, protocol = future.result()
        # TODO : handle pinging
        # if service_type == TCP:
        #     pinger = Pinger(self, asyncio.get_event_loop())
        #     self._pingers[node_id] = pinger
        #     pinger.register_tcp_service(protocol, node_id)
        #     asyncio.async(pinger.start_ping())
        self._client_protocols[node_id] = protocol

    @staticmethod
    def _create_json_service_name(app, service, version):
        return {'app': app, 'service': service, 'version': version}

    @staticmethod
    def _handle_ping(packet, protocol):
        protocol.send(ControlPacket.pong(packet['node_id']))

    def _handle_pong(self, node_id, count):
        pinger = self._pingers[node_id]
        asyncio.async(pinger.pong_received(count))

    def _clear_request_queue(self):
        self._pending_requests[:] = [each for each in self._pending_requests if not self._send_packet(each)]

    def _send_packet(self, packet):
        node_id = self._get_node_id_for_packet(packet)
        if node_id is not None:
            client_protocol = self._client_protocols[node_id]
            if client_protocol.is_connected():
                packet['to'] = node_id
                client_protocol.send(packet)
                return True
            else:
                return False
        return False

    def _get_node_id_for_packet(self, packet):
        app, service, version, entity = packet['app'], packet['service'], packet['version'], packet['entity']
        node = self._registry_client.resolve(service, version, entity, TCP)
        return node[2] if node else None

    def handle_ping_timeout(self, node_id):
        _logger.info("Service client connection timed out {}".format(node_id))
        self._pingers.pop(node_id, None)
        service_props = self._registry_client.get_for_node(node_id)
        _logger.info('service client props {}'.format(service_props))
        if service_props is not None:
            host, port, _node_id, _type = service_props
            asyncio.async(self._connect_to_client(host, _node_id, port, _type))

    def receive(self, packet: dict, protocol, transport):
        if packet['type'] == 'ping':
            self._handle_ping(packet, protocol)
        elif packet['type'] == 'pong':
            self._handle_pong(packet['node_id'], packet['count'])
        elif packet['type'] == 'publish':
            self._handle_publish(packet, protocol)
        else:
            if self.tcp_host.is_for_me(packet['service'], packet['version']):
                func = getattr(self, '_' + packet['type'] + '_receiver')
                func(packet, protocol)
            else:
                _logger.warn('wrongly routed packet: ', packet)

    def _request_receiver(self, packet, protocol):
        api_fn = getattr(self.tcp_host, packet['endpoint'])
        if api_fn.is_api:
            from_node_id = packet['from']
            entity = packet['entity']
            future = asyncio.async(api_fn(from_id=from_node_id, entity=entity, **packet['payload']))

            def send_result(f):
                result_packet = f.result()
                protocol.send(result_packet)

            future.add_done_callback(send_result)
        else:
            print('no api found for packet: ', packet)

    def _handle_publish(self, packet, protocol):
        service, version, endpoint, payload, publish_id = packet['service'], packet['version'], packet['endpoint'], \
                                                          packet['payload'], packet['publish_id']
        for client in self._service_clients:
            if client.name == service and client.version == version:
                fun = getattr(client, endpoint)
                asyncio.async(fun(payload))
        protocol.send(MessagePacket.ack(publish_id))


class PubSubBus:
    PUBSUB_DELAY = 5

    def __init__(self, registry_client):
        self._pubsub_handler = None
        self._registry_client = registry_client
        self._clients = None
        self._pending_publishes = {}

    def create_pubsub_handler(self, host, port):
        self._pubsub_handler = PubSub(host, port)
        yield from self._pubsub_handler.connect()

    def register_for_subscription(self, clients):
        self._clients = clients
        subscription_list = []
        xsubscription_list = []
        for client in clients:
            if isinstance(client, TCPServiceClient):
                for each in dir(client):
                    fn = getattr(client, each)
                    if callable(fn) and getattr(fn, 'is_subscribe', False):
                        subscription_list.append(self._get_pubsub_key(client.name, client.version, fn.__name__))
                    elif callable(fn) and getattr(fn, 'is_xsubscribe', False):
                        xsubscription_list.append((client.name, client.version, fn.__name__, getattr(fn, 'strategy')))
        self._registry_client.x_subscribe(xsubscription_list)
        yield from self._pubsub_handler.subscribe(subscription_list, handler=self.subscription_handler)

    def publish(self, service, version, endpoint, payload):
        endpoint_key = self._get_pubsub_key(service, version, endpoint)
        asyncio.async(self._retry_publish(endpoint_key, json.dumps(payload, cls=VykedEncoder)))
        publish_id = str(uuid.uuid4())
        future = asyncio.async(self.xpublish(publish_id, service, version, endpoint, payload))
        self._pending_publishes[publish_id] = future

    def xpublish(self, publish_id, service, version, endpoint, payload):
        subscribers = yield from self._registry_client.get_subscribers(service, version, endpoint)
        strategies = defaultdict(list)
        for subscriber in subscribers:
            strategies[(subscriber['service'], subscriber['version'])].append(
                (subscriber['host'], subscriber['port'], subscriber['node_id'], subscriber['strategy']))
        if not len(subscribers):
            future = self._pending_publishes[publish_id]
            future.cancel()
            return
        yield from self._connect_and_publish(publish_id, service, version, endpoint, strategies, payload)
        yield from asyncio.sleep(self.PUBSUB_DELAY)
        yield from self.xpublish(publish_id, service, version, endpoint, payload)

    def receive(self, packet, transport, protocol):
        if packet['type'] == 'ack':
            future = self._pending_publishes.pop(packet['request_id'])
            future.cancel()
            transport.close()

    def _retry_publish(self, endpoint, payload):
        return (yield from self._pubsub_handler.publish(endpoint, payload))

    def subscription_handler(self, endpoint, payload):
        service, version, endpoint = endpoint.split('/')
        client = [sc for sc in self._clients if (sc.name == service and sc.version == version)][0]
        func = getattr(client, endpoint)
        asyncio.async(func(**json.loads(payload)))

    @staticmethod
    def _get_pubsub_key(service, version, endpoint):
        return '/'.join((service, str(version), endpoint))

    def _connect_and_publish(self, publish_id, service, version, endpoint, strategies, payload):
        for key, value in strategies.items():
            if value[0][3] == 'LEADER':
                host, port = value[0][0], value[0][1]
            else:
                random_metadata = random.choice(value)
                host, port = random_metadata[0], random_metadata[1]
            transport, protocol = yield from asyncio.get_event_loop().create_connection(partial(get_vyked_protocol, self), host, port)
            packet = MessagePacket.publish(publish_id, service, version, endpoint, payload)
            protocol.send(packet)
