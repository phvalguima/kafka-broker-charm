#!/usr/bin/env python3
# Copyright 2021 pguimaraes
# See LICENSE file for licensing details.

import base64
import subprocess
import logging
import os
import yaml

from ops.charm import CharmBase
from ops.main import main
from ops.framework import StoredState
from ops.model import MaintenanceStatus, ActiveStatus
from ops.model import BlockedStatus

from charmhelpers.fetch import (
    apt_update,
    add_source,
    apt_install
)
from charmhelpers.core.templating import render
from charmhelpers.core.hookenv import (
    is_leader
)

from charmhelpers.core.host import (
    service_running,
    service_restart,
    service_reload
)

from wand.apps.kafka import KafkaJavaCharmBase
from .cluster import KafkaBrokerCluster
from wand.apps.relations.zookeeper import ZookeeperRequiresRelation
from wand.security.ssl import (
    genRandomPassword,
    generateSelfSigned,
    PKCS12CreateKeystore
)
from wand.contrib.linux import getCurrentUserAndGroup


logger = logging.getLogger(__name__)

# Given: https://docs.confluent.io/current/ \
#        installation/cp-ansible/ansible-configure.html
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


class KafkaBrokerCharm(KafkaJavaCharmBase):

    def _install_tarball(self):
        # Use _generate_service_files here
        raise Exception("Not Implemented Yet")

    def __init__(self, *args):
        super().__init__(*args)
        self.framework.observe(self.on.install, self._on_install)
        self.framework.observe(self.on.config_changed, self._on_config_changed)
        self.framework.observe(self.on.cluster_relation_joined,
                               self._on_cluster_relation_joined)
        self.framework.observe(self.on.cluster_relation_changed,
                               self._on_cluster_relation_changed)
        self.framework.observe(self.on.zookeeper_relation_joined,
                               self._on_zookeeper_relation_joined)
        self.framework.observe(self.on.zookeeper_relation_changed,
                               self._on_zookeeper_relation_changed)
        self.cluster = KafkaBrokerCluster(self, 'cluster')
        self.zk = ZookeeperRequiresRelation(self, 'zookeeper')
        self.ks.set_default(zk_cert="")
        self.ks.set_default(zk_key="")
        self.ks.set_default(ssl_cert="")
        self.ks.set_default(ssl_key="")
        os.makedirs("/var/ssl/private", exist_ok=True)
        self._generate_keystores()

    def _on_cluster_relation_joined(self, event):
        self.cluster.on_cluster_relation_joined(event)
        self._on_config_changed(event)

    def _on_cluster_relation_changed(self, event):
        self.cluster.on_cluster_relation_changed(event)
        self._on_config_changed(event)

    def _on_zookeeper_relation_joined(self, event):
        self.zk.user = self.config.get("user", "")
        self.zk.group = self.config.get("group", "")
        self.zk.mode = 0o640
        self.zk.on_zookeeper_relation_joined(event)
        self._on_config_changed(event)

    def _on_zookeeper_relation_changed(self, event):
        self.zk.user = self.config.get("user", "")
        self.zk.group = self.config.get("group", "")
        self.zk.mode = 0o640
        self.zk.on_zookeeper_relation_changed(event)
        self._on_config_changed(event)

    def _generate_keystores(self):
        # If we will auto-generate the root ca
        # and at least one of the certs or keys is not yet set,
        # then we can proceed and regenerate it.
        if self.config["generate-root-ca"] and \
            (len(self.ks.ssl_cert) > 0 and \
             len(self.ks.ssl_key) > 0 and \
             len(self.ks.zk_cert) > 0 and \
             len(self.ks.zk_key) > 0):
            return
        if self.config["generate-root-ca"]:
            self.ks.ssl_cert, self.ks.ssl_key = \
                generateSelfSigned(self.unit_folder,
                                   certname="zk-kafka-broker-root-ca",
                                   user=self.config["user"],
                                   group=self.config["group"],
                                   mode=0o640)
            self.ks.zk_cert, self.ks.zk_key = \
                generateSelfSigned(self.unit_folder,
                                   certname="ssl-kafka-broker-root-ca",
                                   user=self.config["user"],
                                   group=self.config["group"],
                                   mode=0o640)
        else:
            # Check if the certificates remain the same
            if self.ks.zk_cert == self.get_zk_cert() and \
                self.ks.zk_key == self.get_zk_key() and \
                self.ks.ssl_cert == self.get_ssl_cert() and \
                self.ks.ssl_key == self.get_ssl_key():
                # Yes, they do, leave this method as there is nothing to do.
                return
            # Certs already set either as configs or certificates relation
            self.ks.zk_cert = self.get_zk_cert()
            self.ks.zk_key = self.get_zk_key()
            self.ks.ssl_cert = self.get_ssl_cert()
            self.ks.ssl_key = self.get_ssl_key()
        if (len(self.ks.zk_cert) > 0 and len(self.ks.zk_key) > 0):
            self.ks.ks_zookeeper_pwd = genRandomPassword()
            filename = genRandomPassword(6)
            PKCS12CreateKeystore(
                self.get_zk_keystore(),
                self.ks.ks_zookeeper_pwd,
                self.get_zk_cert(),
                self.get_zk_key(),
                user=self.config["user"],
                group=self.config["group"],
                mode=0o640,
                openssl_chain_path="/tmp/" + filename + ".chain",
                openssl_key_path="/tmp/" + filename + ".key",
                openssl_p12_path="/tmp/" + filename + ".p12")
        if len(self.ks.ssl_cert) > 0 and \
           len(self.ks.ssl_key) > 0:
            self.ks.ts_zookeeper_pwd = genRandomPassword()
            filename = genRandomPassword(6)
            PKCS12CreateKeystore(
                self.get_ssl_keystore(),
                self.ks.ks_password,
                self.get_ssl_cert(),
                self.get_ssl_key(),
                user=self.config["user"],
                group=self.config["group"],
                mode=0o640,
                openssl_chain_path="/tmp/" + filename + ".chain",
                openssl_key_path="/tmp/" + filename + ".key",
                openssl_p12_path="/tmp/" + filename + ".p12")

    def _on_install(self, event):
        # TODO: Create /var/lib/kafka folder and all log dirs, set permissions
        packages = []
        if self.config.get("install_method") == "archive":
            self._install_tarball()
        else:
            if self.distro == "confluent":
                packages = CONFLUENT_PACKAGES
            else:
                raise Exception("Not Implemented Yet")
            super().install_packages('openjdk-11-headless', packages)
        # The logic below avoid an error such as more than one entry
        # In this case, we will pick the first entry
        data_log_fs = list(self.config["data-log-dir"].items())[0][0]
        data_log_dir = list(self.config["data-log-dir"].items())[0][1]
        self.create_log_dir(self.config["data-log-device"],
                                      data_log_dir,
                                      data_log_fs,
                                      self.config.get("user",
                                                      "cp-kafka"),
                                      self.config.get("group",
                                                      "confluent"),
                                      self.config.get("fs-options", None))
        self._on_config_changed(event)

    def _check_if_ready(self):
        if not self.cluster.is_ready:
            BlockedStatus("Waiting for cluster relation")
            return
        if not service_running(self.service):
            BlockedStatus("Service not running {}".format(self.service))
            return
        ActiveStatus("{} running".format(self.service))

    def _rel_get_remote_units(self, rel_name):
        return self.framework.model.get_relation(rel_name).units

    def get_ssl_cert(self):
        if self.config["generate-root-ca"]:
            return self.ks.ssl_cert
        return base64.b64decode(self.config["ssl_cert"]).decode("ascii")

    def get_ssl_key(self):
        if self.config["generate-root-ca"]:
            return self.ks.ssl_key
        return base64.b64decode(self.config["ssl_key"]).decode("ascii")

    def get_ssl_keystore(self):
        path = self.config.get("keystore-path",
                               "/var/ssl/private/kafka_ssl_ks.jks")
        pwd = self.ks.ks_password
        return path

    def get_ssl_truststore(self):
        path = self.config.get("truststore-path",
                               "/var/ssl/private/kafka_ssl_ks.jks")
        pwd = self.ks.ts_password
        return path

    def get_zk_keystore(self):
        path = self.config.get("keystore-zookeeper-path",
                               "/var/ssl/private/kafka_zk_ks.jks")
        return path

    def get_zk_truststore(self):
        path = self.config.get("truststore-zookeeper-path",
                               "/var/ssl/private,kafka_zk_ts.jks")
        return path

    def get_zk_cert(self):
        # TODO(pguimaraes): expand it to count
        # with certificates relation or action cert/key
        if self.config["generate-root-ca"]:
            return self.ks.zk_cert
        return base64.b64decode(
                   self.config["ssl-zk-cert"]).decode("ascii")

    def get_zk_key(self):
        # TODO(pguimaraes): expand it to count
        # with certificates relation or action cert/key
        if self.config["generate-root-ca"]:
            return self.ks.zk_key
        return base64.b64decode(
                   self.config["ssl-zk-key"]).decode("ascii")

    def _generate_server_properties(self):
        # TODO: set confluent.security.event.logger.exporter.kafka.topic.replicas
        server_props = self.config.get("server-properties", "")
        server_props["log.dirs"] = \
            list(yaml.safe_load(self.config.get("log.dirs",{})).items())[0][1]
        if os.environ.get("JUJU_AVAILABILITY_ZONE", None) and \
            self.config["customize-failure-domain"]:
            server_props["broker.rack"] = \
                os.environ.get("JUJU_AVAILABILITY_ZONE")
        # Resolve replication factors
        replication_factor = self.config.get("replication-factor",3)
        server_props["offsets.topic.replication.factor"] = replication_factor
        if replication_factor > self.cluster.num_peers or \
            (replication_factor > self.cluster.num_azs and \
             self.config["customize-failure-domain"]):
            BlockedStatus("Not enough brokers " +
                          "(or AZs, if customize-failure-domain is set)")
            return

        server_props["transaction.state.log.min.isr"] = \
            min(2, replication_factor)
        server_props["transaction.state.log.replication.factor"] = \
            replication_factor
        if self.distro == "confluent":
            server_props["confluent.license.topic.replication.factor"] = \
                replication_factor
            server_props["confluent.metadata.topic.replication.factor"] = \
                replication_factor
            server_props["confluent.balancer.topic.replication.factor"] = \
                replication_factor
        server_props = { **server_props, **self.cluster.listener_opts }
        # TODO: Enable rest proxy if we have RBAC:
        # https://github.com/confluentinc/cp-ansible/blob/ \
        #     b711fc9e3b43d2069a9ac8b13177e7f2a07c7bfb/VARIABLES.md
        server_props["kafka_broker_rest_proxy_enabled"] = False
        # Cluster certificate:
        if self.config.get("generate-root-ca", False) or \
            (self.ks.ssl_cert and self.ks.ssl_key):
            user = self.config.get("user", "")
            group = self.config.get("group", "")
            ks = self.get_ssl_keystore()
            ts = self.get_ssl_truststore()
            self.cluster.set_ssl_keypair(self.ks.ssl_cert,
                                         self.get_ssl_keystore(),
                                         self.ks.ks_password,
                                         self.get_ssl_truststore(),
                                         self.ks.ts_password,
                                         user, group, 0o640)
        # Zookeeper options:
        if self.ks.zk_cert and self.ks.zk_key:
            user, group = getCurrentUserAndGroup()
            self.zk.user = self.config.get("user", user)
            self.zk.group = self.config.get("group", group)
            self.zk.mode = 0o640
            self.zk.set_mTLS_auth(
                self.get_zk_cert(),
                self.get_zk_truststore(),
                self.ks.ts_zookeeper_pwd)
        if self.is_sasl_enabled():
            if self.distro == "confluent":
                server_props["authorizer.class.name"] = "io.confluent.kafka.security.authorizer.ConfluentServerAuthorizer"
                server_props["confluent.authorizer.access.rule.providers"] = "CONFLUENT,ZK_ACL"
            elif self.distro == "apache":
                raise Exception("Not Implemented Yet")
        server_props["zookeeper.connect"] = self.zk.get_zookeeper_list
        server_props["zookeeper.set.acl"] = self.zk.is_sasl_enabled()

        if self.zk.is_mTLS_enabled():
            # TLS client properties uses the same variables
            # Rendering as a part of server properties
            client_props = {}
            client_props["zookeeper.clientCnxnSocket"] = "org.apache.zookeeper.ClientCnxnSocketNetty"
            client_props["zookeeper.ssl.client.enable"] = "true"
            client_props["zookeeper.ssl.keystore.location"] = \
                self.get_zk_keystore()
            client_props["zookeeper.ssl.keystore.password"] = self.ks.ks_zookeeper_pwd
            client_props["zookeeper.ssl.truststore.location"] = \
                self.get_zk_truststore()
            client_props["zookeeper.ssl.truststore.password"] = self.ks.ts_zookeeper_pwd
            # Set the SSL mTLS config on the relation
            self.zk.set_mTLS_auth(self.get_zk_cert(),
                                  client_props["zookeeper.ssl.truststore.location"],
                                  self.ks.ts_password)
            render(source="zookeeper-tls-client.properties.j2",
                   target="/etc/kafka/zookeeper-tls-client.properties",
                   user=self.config.get('user'),
                   group=self.config.get("group"),
                   perms=0o640,
                   context={
                       "client_props": client_props
                   })
            # Now, rendering the server_props part:
            del client_props["zookeeper.ssl.keystore.location"]
            del client_props["zookeeper.ssl.keystore.password"]
            server_props = {**server_props, **client_props}
        # Back to server.properties, render it
        render(source="server.properties.j2",
               target="/etc/kafka/server.properties",
               user=self.config.get('user'),
               group=self.config.get("group"),
               perms=0o640,
               context={
                   "server_props": server_props
               })

    def is_sasl_enabled(self):
        if self.is_sasl_kerberos_enabled() or \
           self.is_sasl_oauthbearer_enabled() or \
           self.is_sasl_scram_enabled() or \
           self.is_sasl_plain_enabled() or \
           self.is_sasl_delegate_token_enabled() or \
           self.is_sasl_ldap_enabled():
            return True
        return False

    def _generate_client_properties(self):
        client_props = self.config["client-properties"] or {}
        if self.is_sasl_enabled():
            client_props["sasl.jaas.config"] = self.config.get("sasl-jaas-config","")
        if self.is_sasl_kerberos_enabled():
            client_prpos["sasl.mechanism"] = "GSSAPI"
            client_props["sasl.kerberos.service.name"] = self.config.get("sasl-kbros-service", "HTTP")
        if self.is_ssl_enabled():
            client_props["ssl.keystore.location"] = \
                self.config.get("keystore-path",
                                "/var/ssl/private/kafka_ks.jks")
            client_props["ssl.keystore.password"] = self.ks.ks_password
            client_props["ssl.truststore.location"] = \
                self.config.get("truststore-path",
                                "/var/ssl/private/kafka_ts.jks")
            client_props["ssl.truststore.password"] = self.ks.ts_password
        render(source="client.properties.j2",
               target="/etc/kafka/client.properties",
               user=self.config.get('user'),
               group=self.config.get("group"),
               perms=0o640,
               context={
                   "client_props": client_props
               })

    def _on_config_changed(self, _):
        if self.distro == "confluent":
            self.service = "confluent-server"
        else:
            self.service = "kafka"
        self._generate_keystores()
        self._generate_server_properties()
        self._generate_client_properties()
        self.render_service_override_file()
        service_reload(self.service)
        service_running(self.service)
        self._check_if_ready()
        # Apply sysctl



if __name__ == "__main__":
    main(KafkaBrokerCharm)
