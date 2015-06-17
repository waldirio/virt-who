"""
Module for communcating with subscription-manager, part of virt-who

Copyright (C) 2011 Radek Novacek <rnovacek@redhat.com>

This program is free software; you can redistribute it and/or
modify it under the terms of the GNU General Public License
as published by the Free Software Foundation; either version 2
of the License, or (at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program; if not, write to the Free Software
Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.
"""

import os
import json
from httplib import BadStatusLine

import rhsm.connection as rhsm_connection
import rhsm.certificate as rhsm_certificate
import rhsm.config as rhsm_config

from ..manager import Manager, ManagerError, ManagerFatalError


class SubscriptionManagerError(ManagerError):
    pass


class SubscriptionManagerUnregisteredError(ManagerFatalError):
    pass


class SubscriptionManager(Manager):
    smType = "sam"

    """ Class for interacting subscription-manager. """
    def __init__(self, logger, options, manager_queue=None):
        self.logger = logger
        self.options = options
        self.cert_uuid = None
        self.manager_queue = manager_queue

        self.rhsm_config = rhsm_config.initConfig(rhsm_config.DEFAULT_CONFIG_PATH)
        self.readConfig()

    def readConfig(self):
        """ Parse rhsm.conf in order to obtain consumer
            certificate and key paths. """
        consumerCertDir = self.rhsm_config.get("rhsm", "consumerCertDir")
        cert = 'cert.pem'
        key = 'key.pem'
        self.cert_file = os.path.join(consumerCertDir, cert)
        self.key_file = os.path.join(consumerCertDir, key)

    def _connect(self, rhsm_username=None, rhsm_password=None):
        """ Connect to the subscription-manager. """
        kwargs = {
            'host': self.rhsm_config.get('server', 'hostname'),
            'ssl_port': int(self.rhsm_config.get('server', 'port')),
            'handler': self.rhsm_config.get('server', 'prefix'),
            'proxy_hostname': self.rhsm_config.get('server', 'proxy_hostname'),
            'proxy_port': self.rhsm_config.get('server', 'proxy_port'),
            'proxy_user': self.rhsm_config.get('server', 'proxy_user'),
            'proxy_password': self.rhsm_config.get('server', 'proxy_password')
        }

        if rhsm_username and rhsm_password:
            self.logger.debug("Authenticating with RHSM username %s" % rhsm_username)
            kwargs['username'] = rhsm_username
            kwargs['password'] = rhsm_password
        else:
            self.logger.debug("Authenticating with certificate: %s" % self.cert_file)
            if not os.access(self.cert_file, os.R_OK):
                raise SubscriptionManagerUnregisteredError(
                    "Unable to read certificate, system is not registered or you are not root")
            kwargs['cert_file'] = self.cert_file
            kwargs['key_file'] = self.key_file

        self.connection = rhsm_connection.UEPConnection(**kwargs)
        if not self.connection.ping()['result']:
            raise SubscriptionManagerError("Unable to obtain status from server, UEPConnection is likely not usable.")

    def sendVirtGuests(self, guests):
        """
        Update consumer facts with info about virtual guests.

        `guests` is a list of `Guest` instances (or it children).
        """

        self._connect()

        # Sort the list
        guests.sort(key=lambda item: item.uuid)

        serialized_guests = [guest.toDict() for guest in guests]
        self.logger.info("Sending domain info: %s" % json.dumps(serialized_guests, indent=4, sort_keys=True))

        # Send list of guest uuids to the server
        self.connection.updateConsumer(self.uuid(), guest_uuids=serialized_guests)

    def hypervisorCheckIn(self, config, mapping, type=None):
        """ Send hosts to guests mapping to subscription manager. """
        serialized_mapping = {}
        for host, guests in mapping.items():
            serialized_mapping[host] = [guest.toDict() for guest in guests]

        kwargs = {}
        if config.rhsm_username and config.rhsm_password:
            kwargs['rhsm_username'] = config.rhsm_username
            kwargs['rhsm_password'] = config.rhsm_password
        self._connect(**kwargs)
        self.logger.debug("Checking if server has capability 'hypervisor_async'")
        is_async = self.connection.has_capability('hypervisors_async')

        if (is_async):
            self.logger.debug("Server has capability 'hypervisors_async'")
            # Transform the mapping into the async version
            # FIXME: Should this be here or as part of one of the reports
            new_mapping = {'hypervisors':[]}
            for host, guests in serialized_mapping.iteritems():
                new_mapping['hypervisors'].append({'hypervisorId':host, 'guestIds':guests})
            serialized_mapping = new_mapping
        else:
            self.logger.debug("Server does not have 'hypervisors_async' capability")
            # Do nothing else as the data we have already is in the form that we need

        self.logger.info("Sending update in hosts-to-guests mapping: %s" % json.dumps(serialized_mapping, indent=4, sort_keys=True))
        try:
            result = self.connection.hypervisorCheckIn(config.owner, config.env, serialized_mapping)
        except BadStatusLine:
            raise ManagerError("Communication with subscription manager interrupted")

        if (is_async and self.manager_queue is not None):
            self.manager_queue.put(('newJobStatus', [config, result['id']]))
        return result

    def checkJobStatus(self, config, job_id):
        kwargs = {}
        if config.rhsm_username and config.rhsm_password:
            kwargs['rhsm_username'] = config.rhsm_username
            kwargs['rhsm_password'] = config.rhsm_password
        self._connect(**kwargs)
        self.logger.debug('checking job status\nJob ID: %s' % job_id)
        result = self.connection.getJob(job_id)
        if result['state'] != 'FINISHED':
            # This will cause the managerprocess to do this again in 10 seconds
            self.manager_queue.put(('newJobStatus', [config, result['id']]))
        else:
            # log completed job status
            # TODO Make this its own method inside a class
            # representing a status object
            resultData = result['resultData']
            for fail in resultData['failedUpdate']:
                self.logger.error("Error during update list of guests: %s", str(fail))
            for updated in resultData['updated']:
                guests = [x['guestId'] for x in updated['guestIds']]
                self.logger.info("Updated host %s with guests: [%s]",
                                 updated['uuid'],
                                 ", ".join(guests))
            if (isinstance(result['created'], list)):
                for created in result['created']:
                    guests = [x['guestId'] for x in created['guestIds']]
                    self.logger.info("Created host: %s with guests: [%s]",
                                     created['uuid'],
                                     ", ".join(guests))
            result = resultData
        return result

    def uuid(self):
        """ Read consumer certificate and get consumer UUID from it. """
        if not self.cert_uuid:
            try:
                certificate = rhsm_certificate.create_from_file(self.cert_file)
                self.cert_uuid = certificate.subject["CN"]
            except Exception as e:
                raise SubscriptionManagerError("Unable to open certificate %s (%s):" % (self.cert_file, str(e)))
        return self.cert_uuid
