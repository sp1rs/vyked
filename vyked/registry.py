import signal
import asyncio
from functools import partial
from collections import defaultdict, namedtuple

from .packet import ControlPacket


# TODO : Better objects
Service = namedtuple('Service', ['name', 'version', 'dependencies', 'host', 'port', 'node_id', 'type'])


class Repository:
    def __init__(self):
        self._registered_services = defaultdict(list)
        self._pending_services = defaultdict(list)
        self._service_dependencies = {}

    def register_service(self, service: Service):
        service_name = self._get_full_service_name(service.name, service.version)
        service_entry = (service.host, service.port, service.node_id, service.type)
        self._registered_services[service_name].append(service_entry)
        self._pending_services[service_name].append(service.node_id)
        if self._service_dependencies.get(service_name) is None:
            self._service_dependencies[service_name] = service.dependencies

    def add_pending_service(self, service, version, node_id):
        self._pending_services[self._get_full_service_name(service, version)].append(node_id)

    def get_pending_services(self):
        return [self._split_key(key) for key in self._pending_services.keys()]

    def get_pending_instances(self, service, version):
        return self._pending_services.get(self._get_full_service_name(service, version), [])

    def remove_pending_instance(self, service, version, node_id):
        self.get_pending_instances(service, version).remove(node_id)

    def get_instances(self, service, version):
        service_name = self._get_full_service_name(service, version)
        return self._registered_services.get(service_name, [])

    def get_consumers(self, service_name, service_version):
        consumers = []
        for service, vendors in self._service_dependencies.items():
            for each in vendors:
                if each['service'] == service_name and each['version'] == service_version:
                    consumers.append(self._split_key(service))
        return consumers

    def get_vendors(self, service, version):
        return self._service_dependencies.get(self._get_full_service_name(service, version), [])

    def get_node(self, node_id):
        for service, instances in self._registered_services.items():
            for host, port, node, service_type in instances:
                if node_id == node:
                    name, version = self._split_key(service)
                    return Service(name, version, [], host, port, node, service_type)
        return None

    @staticmethod
    def _get_full_service_name(service: str, version):
        return '{}/{}'.format(service, version)

    @staticmethod
    def _split_key(key: str):
        return tuple(key.split('/'))


class Registry:
    def __init__(self, ip, port):
        self._ip = ip
        self._port = port
        self._loop = asyncio.get_event_loop()
        self._client_protocols = {}
        self._service_protocols = {}
        self._repository = Repository()

    def _rfactory(self):
        from vyked.jsonprotocol import RegistryProtocol

        return RegistryProtocol(self)

    def start(self):
        self._loop.add_signal_handler(getattr(signal, 'SIGINT'), partial(self._stop, 'SIGINT'))
        self._loop.add_signal_handler(getattr(signal, 'SIGTERM'), partial(self._stop, 'SIGTERM'))
        registry_coro = self._loop.create_server(self._rfactory, self._ip, self._port)
        self._server = self._loop.run_until_complete(registry_coro)
        try:
            self._loop.run_forever()
        except Exception as e:
            print(e)
        finally:
            self._server.close()
            self._loop.run_until_complete(self._server.wait_closed())
            self._loop.close()

    def _stop(self, signame:str):
        print('\ngot signal {} - exiting'.format(signame))
        self._loop.stop()

    def receive(self, packet: dict, registry_protocol, transport):
        request_type = packet['type']
        if request_type == 'register':
            self.register_service(packet, registry_protocol, *transport.get_extra_info('peername'))
        elif request_type == 'get_instances':
            self.get_service_instances(packet, registry_protocol)

    def deregister_service(self, node_id):
        service = self._repository.get_node(node_id)
        if service is not None:
            self._service_protocols.pop(node_id, None)
            self._client_protocols.pop(node_id, None)
            self._notify_consumers(service.name, service.version, node_id)
            if not len(self._repository.get_instances(service.name, service.version)):
                consumer_name, consumer_version = self._repository.get_consumers(service.name, service.version)
                for _, _, node_id, _ in self._repository.get_instances(service.name, service.version):
                    self._repository.add_pending_service(consumer_name, consumer_version, node_id)

    def register_service(self, packet: dict, registry_protocol, host, port):
        params = packet['params']
        service = Service(params['service'], params['version'], params['vendors'], host, params['port'],
                          params['node_id'], params['type'])
        self._repository.register_service(service)
        self._client_protocols[params['node_id']] = registry_protocol
        self._connect_to_service(host, params['port'], params['node_id'], params['type'])
        self._handle_pending_registrations()

    def _send_activated_packet(self, service, version, node):
        protocol = self._client_protocols[node]
        packet = self._make_activated_packet(service, version)
        protocol.send(packet)

    def _handle_pending_registrations(self):
        for service, version in self._repository.get_pending_services():
            vendors = self._repository.get_vendors(service, version)
            should_activate = True
            for vendor in vendors:
                if not len(self._repository.get_instances(vendor['service'], vendor['version'])):
                    should_activate = False
                    break
            for node in self._repository.get_pending_instances(service, version):
                if should_activate:
                    self._send_activated_packet(service, version, node)
                    self._repository.remove_pending_instance(service, version, node)

    def _make_activated_packet(self, service, version):
        vendors = self._repository.get_vendors(service, version)
        instances = {
            (vendor['service'], vendor['version']): self._repository.get_instances(vendor['service'], vendor['version']) for
            vendor in vendors}
        return ControlPacket.activated(instances)

    def _connect_to_service(self, host, port, node_id, service_type):
        if service_type == 'tcp':
            coro = self._loop.create_connection(self._rfactory, host, port)
            future = asyncio.async(coro)
            future.add_done_callback(partial(self._handle_service_connection, node_id))

    def _handle_service_connection(self, node_id, future):
        transport, protocol = future.result()
        self._service_protocols[node_id] = protocol

    def _notify_consumers(self, service, version, node_id):
        packet = ControlPacket.deregister(service, version, node_id)
        for consumer_name, consumer_version in self._repository.get_consumers(service, version):
            for host, port, node, service_type in self._repository.get_instances(consumer_name, consumer_version):
                protocol = self._client_protocols[node]
                protocol.send(packet)

    def get_service_instances(self, packet, registry_protocol):
        params = packet['params']
        service, version = params['service'], params['version']
        instances = self._repository.get_consumers(service, version)
        instance_packet = ControlPacket.send_instances(service, version, instances)
        registry_protocol.send(instance_packet)


if __name__ == '__main__':
    from setproctitle import setproctitle

    setproctitle("registry")
    REGISTRY_HOST = None
    REGISTRY_PORT = 4500
    registry = Registry(REGISTRY_HOST, REGISTRY_PORT)
    registry.start()
