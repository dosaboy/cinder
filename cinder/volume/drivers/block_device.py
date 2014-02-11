# Copyright (c) 2013 Mirantis, Inc.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import os

from oslo.config import cfg

from cinder.brick.iscsi import iscsi
from cinder import context
from cinder.db.sqlalchemy import api
from cinder import exception
from cinder.image import image_utils
from cinder.openstack.common import log as logging
from cinder import utils
from cinder.volume import driver
from cinder.volume import utils as volutils


LOG = logging.getLogger(__name__)

volume_opts = [
    cfg.ListOpt('available_devices',
                default=[],
                help='List of all available devices'),
]

CONF = cfg.CONF
CONF.register_opts(volume_opts)


class BlockDeviceDriver(driver.ISCSIDriver):
    VERSION = '1.0.0'

    def __init__(self, *args, **kwargs):
        self.target_helper = self.get_target_helper()

        super(BlockDeviceDriver, self).__init__(*args, **kwargs)
        self.configuration.append_config_values(volume_opts)

    def set_execute(self, execute):
        super(BlockDeviceDriver, self).set_execute(execute)
        self.target_helper.set_execute(execute)

    def check_for_setup_error(self):
        pass

    def create_volume(self, volume):
        device = self.find_appropriate_size_device(volume['size'])
        LOG.info("Create %s on %s" % (volume['name'], device))
        return {
            'provider_location': self._iscsi_location(None, None, None, None,
                                                      device),
        }

    def initialize_connection(self, volume, connector):
        if connector['host'] != volume['host']:
            return super(BlockDeviceDriver, self). \
                initialize_connection(volume, connector)
        else:
            return {
                'driver_volume_type': 'local',
                'data': {'device_path': self.local_path(volume)},
            }

    def terminate_connection(self, volume, connector, **kwargs):
        pass

    def create_export(self, context, volume):
        """Creates an export for a logical volume."""

        iscsi_name = "%s%s" % (self.configuration.iscsi_target_prefix,
                               volume['name'])
        volume_path = self.local_path(volume)
        model_update = {}

        # TODO(jdg): In the future move all of the dependent stuff into the
        # corresponding target admin class
        if not isinstance(self.target_helper, iscsi.TgtAdm):
            lun = 0
            self._ensure_iscsi_targets(context, volume['host'])
            iscsi_target = self.db.volume_allocate_iscsi_target(context,
                                                                volume['id'],
                                                                volume['host'])
        else:
            lun = 1  # For tgtadm the controller is lun 0, dev starts at lun 1
            iscsi_target = 0  # NOTE(jdg): Not used by tgtadm

        # Use the same method to generate the username and the password.
        chap_username = utils.generate_username()
        chap_password = utils.generate_password()
        chap_auth = self._iscsi_authentication('IncomingUser', chap_username,
                                               chap_password)
        # NOTE(jdg): For TgtAdm case iscsi_name is the ONLY param we need
        # should clean this all up at some point in the future
        tid = self.target_helper.create_iscsi_target(iscsi_name,
                                                     iscsi_target,
                                                     0,
                                                     volume_path,
                                                     chap_auth)
        model_update['provider_location'] = self._iscsi_location(
            self.configuration.iscsi_ip_address, tid, iscsi_name, lun,
            volume_path)
        model_update['provider_auth'] = self._iscsi_authentication(
            'CHAP', chap_username, chap_password)
        return model_update

    def remove_export(self, context, volume):
        """Removes an export for a logical volume."""
        # NOTE(jdg): tgtadm doesn't use the iscsi_targets table
        # TODO(jdg): In the future move all of the dependent stuff into the
        # corresponding target admin class

        if isinstance(self.target_helper, iscsi.LioAdm):
            try:
                iscsi_target = self.db.volume_get_iscsi_target_num(
                    context,
                    volume['id'])
            except exception.NotFound:
                LOG.info(_("Skipping remove_export. No iscsi_target "
                           "provisioned for volume: %s"), volume['id'])
                return
            self.target_helper.remove_iscsi_target(iscsi_target,
                                                   0,
                                                   volume['id'],
                                                   volume['name'])
            return
        elif not isinstance(self.target_helper, iscsi.TgtAdm):
            try:
                iscsi_target = self.db.volume_get_iscsi_target_num(
                    context,
                    volume['id'])
            except exception.NotFound:
                LOG.info(_("Skipping remove_export. No iscsi_target "
                           "provisioned for volume: %s"), volume['id'])
                return
        else:
            iscsi_target = 0
        try:
            # NOTE: provider_location may be unset if the volume hasn't
            # been exported
            location = volume['provider_location'].split(' ')
            iqn = location[1]
            # ietadm show will exit with an error
            # this export has already been removed
            self.target_helper.show_target(iscsi_target, iqn=iqn)
        except Exception:
            LOG.info(_("Skipping remove_export. No iscsi_target "
                       "is presently exported for volume: %s"), volume['id'])
            return
        self.target_helper.remove_iscsi_target(iscsi_target, 0, volume['id'],
                                               volume['name'])

    def ensure_export(self, context, volume):
        """Synchronously recreates an export for a logical volume.
        :param context:
        :param volume:
        """
        # NOTE(jdg): tgtadm doesn't use the iscsi_targets table
        # TODO(jdg): In the future move all of the dependent stuff into the
        # corresponding target admin class

        if isinstance(self.target_helper, iscsi.LioAdm):
            try:
                volume_info = self.db.volume_get(context, volume['id'])
                (auth_method,
                 auth_user,
                 auth_pass) = volume_info['provider_auth'].split(' ', 3)
                chap_auth = self._iscsi_authentication(auth_method,
                                                       auth_user,
                                                       auth_pass)
            except exception.NotFound:
                LOG.debug("volume_info:", volume_info)
                LOG.info(_("Skipping ensure_export. No iscsi_target "
                           "provision for volume: %s"), volume['id'])
                return
            iscsi_name = "%s%s" % (self.configuration.iscsi_target_prefix,
                                   volume['name'])
            volume_path = self.local_path(volume)
            iscsi_target = 1
            self.target_helper.create_iscsi_target(iscsi_name, iscsi_target,
                                                   0, volume_path, chap_auth,
                                                   check_exit_code=False)
            return
        if not isinstance(self.target_helper, iscsi.TgtAdm):
            try:
                iscsi_target = self.db.volume_get_iscsi_target_num(
                    context,
                    volume['id'])
            except exception.NotFound:
                LOG.info(_("Skipping ensure_export. No iscsi_target "
                           "provisioned for volume: %s"), volume['id'])
                return
        else:
            iscsi_target = 1  # dummy value when using TgtAdm

        chap_auth = None

        # Check for https://bugs.launchpad.net/cinder/+bug/1065702
        old_name = None
        volume_name = volume['name']

        iscsi_name = "%s%s" % (self.configuration.iscsi_target_prefix,
                               volume_name)
        volume_path = self.local_path(volume)

        # NOTE(jdg): For TgtAdm case iscsi_name is the ONLY param we need
        # should clean this all up at some point in the future
        self.target_helper.create_iscsi_target(iscsi_name, iscsi_target,
                                               0, volume_path, chap_auth,
                                               check_exit_code=False,
                                               old_name=old_name)

    def _iscsi_location(self, ip, target, iqn, lun=None, device=None):
        return "%s:%s,%s %s %s %s" % (ip, self.configuration.iscsi_port,
                                      target, iqn, lun, device)

    def _iscsi_authentication(self, chap, name, password):
        return "%s %s %s" % (chap, name, password)

    def _ensure_iscsi_targets(self, context, host):
        """Ensure that target ids have been created in datastore."""
        # NOTE(jdg): tgtadm doesn't use the iscsi_targets table
        # TODO(jdg): In the future move all of the dependent stuff into the
        # corresponding target admin class
        if not isinstance(self.target_helper, iscsi.TgtAdm):
            host_iscsi_targets = self.db.iscsi_target_count_by_host(context,
                                                                    host)
            if host_iscsi_targets >= self.configuration.iscsi_num_targets:
                return

            # NOTE(vish): Target ids start at 1, not 0.
            target_end = self.configuration.iscsi_num_targets + 1
            for target_num in xrange(1, target_end):
                target = {'host': host, 'target_num': target_num}
                self.db.iscsi_target_create_safe(context, target)

    def delete_volume(self, volume):
        """Deletes a logical volume."""
        dev_path = self.local_path(volume)
        if not dev_path or dev_path not in \
                self.configuration.available_devices:
            return
        if os.path.exists(dev_path) and \
                self.configuration.volume_clear != 'none':
            volutils.clear_volume(
                self._get_device_size(dev_path), dev_path,
                volume_clear=self.configuration.volume_clear,
                volume_clear_size=self.configuration.volume_clear_size)

    def local_path(self, volume):
        if volume['provider_location']:
            path = volume['provider_location'].split(" ")
            return path[3]
        else:
            return None

    def copy_image_to_volume(self, context, volume, image_service, image_id):
        """Fetch the image from image_service and write it to the volume."""
        image_utils.fetch_to_raw(context,
                                 image_service,
                                 image_id,
                                 self.local_path(volume),
                                 self.configuration.volume_dd_blocksize,
                                 size=volume['size'])

    def copy_volume_to_image(self, context, volume, image_service, image_meta):
        """Copy the volume to the specified image."""
        image_utils.upload_volume(context,
                                  image_service,
                                  image_meta,
                                  self.local_path(volume))

    def create_cloned_volume(self, volume, src_vref):
        LOG.info(_('Creating clone of volume: %s') % src_vref['id'])
        device = self.find_appropriate_size_device(src_vref['size'])
        volutils.copy_volume(
            self.local_path(src_vref), device,
            self._get_device_size(device) * 2048,
            self.configuration.volume_dd_blocksize,
            execute=self._execute)
        return {
            'provider_location': self._iscsi_location(None, None, None, None,
                                                      device),
        }

    def get_volume_stats(self, refresh=False):
        if refresh:
            self._update_volume_stats()
        return self._stats

    def _update_volume_stats(self):
        """Retrieve stats info from volume group."""
        dict_of_devices_sizes = self._devices_sizes()
        used_devices = self._get_used_devices()
        total_size = 0
        free_size = 0
        for device, size in dict_of_devices_sizes.iteritems():
            if device not in used_devices:
                free_size += size
            total_size += size

        LOG.debug("Updating volume stats")
        backend_name = self.configuration.safe_get('volume_backend_name')
        data = {'total_capacity_gb': total_size / 1024,
                'free_capacity_gb': free_size / 1024,
                'reserved_percentage': self.configuration.reserved_percentage,
                'QoS_support': False,
                'volume_backend_name': backend_name or self.__class__.__name__,
                'vendor_name': "Open Source",
                'driver_version': self.VERSION,
                'storage_protocol': 'unknown'}

        self._stats = data

    def _get_used_devices(self):
        lst = api.volume_get_all_by_host(context.get_admin_context(),
                                         self.configuration.host)
        used_devices = set()
        for volume in lst:
            local_path = self.local_path(volume)
            if local_path:
                used_devices.add(local_path)
        return used_devices

    def _get_device_size(self, dev_path):
        out, err = self._execute('blockdev', '--getsz', dev_path,
                                 run_as_root=True)
        size_in_m = int(out)
        return size_in_m / 2048

    def _devices_sizes(self):
        available_devices = self.configuration.available_devices
        dict_of_devices_sizes = {}
        for device in available_devices:
            dict_of_devices_sizes[device] = self._get_device_size(device)
        return dict_of_devices_sizes

    def find_appropriate_size_device(self, size):
        dict_of_devices_sizes = self._devices_sizes()
        free_devices = (set(self.configuration.available_devices) -
                        self._get_used_devices())
        if not free_devices:
            raise exception.CinderException(_("No free disk"))
        possible_device = None
        possible_device_size = None
        for device in free_devices:
            dev_size = dict_of_devices_sizes[device]
            if size * 1024 <= dev_size and (possible_device is None or
                                            dev_size < possible_device_size):
                possible_device = device
                possible_device_size = dev_size

        if possible_device:
            return possible_device
        else:
            raise exception.CinderException(_("No big enough free disk"))