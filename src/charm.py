#!/usr/bin/env python3
# Copyright 2021 pguimaraes
# See LICENSE file for licensing details.

import subprocess
import logging
import yaml

from ops.charm import CharmBase
from ops.main import main
from ops.framework import StoredState
from ops.model import MaintenanceStatus, ActiveStatus

from charmhelpers.fetch import (
    apt_update,
    add_source,
    apt_install
)
from charmhelpers.core import render
from charmhelpers.core.hookenv import (
    is_leader
)

from java import KafkaJavaCharmBase
from cluster import KafkaBrokerCluster
from zookeeper import ZookeeperRequiresRelation

logger = logging.getLogger(__name__)

# Given: https://docs.confluent.io/current/installation/cp-ansible/ansible-configure.html
# Setting confluent-server by default
CONFLUENT_PACKAGES = [
  "confluent-common",
  "confluent-rest-utils",
  "confluent-metadata-service",
  "confluent-ce-kafka-http-server",
  "confluent-kafka-rest",
  "confluent-server-rest",
  "confluent-telemetry",
  "confluent-server",
  "confluent-rebalancer",
  "confluent-security",
]

KAFKA_BROKER_SERVICE_TARGET="/lib/systemd/system/{}.service"

class KafkaBrokerCharm(KafkaJavaCharmBase):
    """Charm the service."""

    def _install_tarball(self):
        # Use _generate_service_files here
        raise Exception("Not Implemented Yet")

    # From: https://github.com/confluentinc/cp-ansible/blob/b711fc9e3b43d2069a9ac8b13177e7f2a07c7bfb/roles/confluent.kafka_broker/defaults/main.yml#L10
    def _collect_java_args(self):
        java_args = ["-Djdk.tls.ephemeralDHKeySize=2048"] # We always use broker listeners
        ## TODO: if JMX Exporter relation, then we should have: -javaagent:{{jmxexporter_jar_path}}={{kafka_broker_jmxexporter_port}}:{{kafka_broker_jmxexporter_config_path}}
        ## TODO: If GSSAPI or SASL enabled, check the extra parameters
        java_args = java_args + self.config.get("java_extra_args","").split()
        return java_args

    # Used for the archive install_method
    def _generate_service_files(self):
        kafka_broker_service_unit_overrides = yaml.safe_load(
            self.config.get('archive-service-unit-overrides',""))
        kafka_broker_service_overrides = yaml.safe_load(
            self.config.get('archive-service-overrides',""))
        kafka_broker_service_environment_overrides = yaml.safe_load(
            self.config.get('archive-service-environment-overrides',""))
        target = None
        if self.distro == "confluent":
            target = "/lib/systemd/system/confluent-server.service"
        elif self.distro == "apache":
            raise Exception("Not Implemented Yet")
        render(source="kafka-broker.service.j2",
               target=target,
               user=self.config.get('kafka-broker-user'),
               group=self.config.get("kafka-broker-group"),
               perms=0o644,
               context={
                   "kafka_broker_service_unit_overrides": kafka_broker_service_unit_overrides,
                   "kafka_broker_service_overrides": kafka_broker_service_overrides,
                   "kafka_broker_service_environment_overrides": kafka_broker_service_environment_overrides
               })

    def __init__(self, *args):
        super().__init__(*args)
        self.framework.observe(self.on.config_changed, self._on_install)
        self.framework.observe(self.on.config_changed, self._on_config_changed)
        self.cluster = KafkaBrokerCluster(self, 'cluster')
        self.zk = ZookeeperRequiresRelation(self, 'zookeeper')

    def _on_install(self, _):
        # TODO: Create /var/lib/kafka folder and all log dirs, set permissions
        packages = []
        if self.config.get("install_method") == "archive":
            self._install_tarball()
        else:
            if self.distro == "confluent":
                packages = CONFLUENT_PACKAGES
            else:
                raise Exception("Not Implemented Yet"
            super().install_packages('openjdk-11-headless', packages)
        self._on_config_changed()


    def _rel_get_remote_units(self, rel_name):
        return self.framework.model.get_relation(rel_name).units

    def _generate_server_properties(self):
        # TODO: set confluent.security.event.logger.exporter.kafka.topic.replicas
        server_props = yaml.safe_load(self.config.get("server-properties", "")) or {}
        if os.environ.get("JUJU_AVAILABILITY_ZONE") and self.config["customize-failure-domain"]:
            server_props["broker.rack"] = os.environ.get("JUJU_AVAILABILITY_ZONE")
        replication_factor = self.config.get("replication-factor",3)
        server_props["offsets.topic.replication.factor"] = replication_factor
        if replication_factor > self.cluster.num_peers or
            (replication_factor > self.cluster.num_azs and self.config["customize-failure-domain"]):
            BlockedStatus("Not enough brokers (or AZs, if customize-failure-domain is set)")
            return
        server_props["transaction.state.log.min.isr"] = min(2, replication_factor)
        server_props["transaction.state.log.replication.factor"] = replication_factor
        if self.distro == "confluent":
            server_props["confluent.license.topic.replication.factor"] = replication_factor
            server_props["confluent.metadata.topic.replication.factor"] = replication_factor
            server_props["confluent.balancer.topic.replication.factor"] = replication_factor
        server_props["zookeeper.connect"] = self.zk.get_zookeeper_list
        server_props = { **server_props, **self.cluster.listeners }
        # TODO: Enable rest proxy if we have RBAC: https://github.com/confluentinc/cp-ansible/blob/b711fc9e3b43d2069a9ac8b13177e7f2a07c7bfb/VARIABLES.md
        server_props["kafka_broker_rest_proxy_enabled"] = False
        render(source="server.properties.j2",
               target="/etc/kafka/server.properties",
               user=self.config.get('kafka-broker-user'),
               group=self.config.get("kafka-broker-group"),
               perms=0o640,
               context={
                   "server_props": server_props
               })

    def _generate_client_properties(self):
        # TODO: it seems we need this just when it comes to client encryption
        # https://github.com/confluentinc/cp-ansible/blob/b711fc9e3b43d2069a9ac8b13177e7f2a07c7bfb/roles/confluent.variables/filter_plugins/filters.py#L159
        pass

    def _on_config_changed(self, _):
        # Note: you need to uncomment the example in the config.yaml file for this to work (ensure
        # to not just leave the example, but adapt to your configuration needs)
        logger.debug("found a new thing: %r", current)
        self._generate_service_files()
        self._generate_server_properties()
        # Apply sysctl



if __name__ == "__main__":
    main(KafkaBrokerCharm)
