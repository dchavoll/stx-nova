# Copyright (c) 2013 Intel, Inc.
# Copyright (c) 2012 OpenStack Foundation
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
#
# Copyright (c) 2013-2017 Wind River Systems, Inc.
#


import errno
import glob
import os
import re

from oslo_log import log as logging


from nova import exception
from nova.network import model as network_model
from nova import utils
from nova.virt import hardware

LOG = logging.getLogger(__name__)

PCI_VENDOR_PATTERN = "^(hex{4})$".replace("hex", "[\da-fA-F]")
PCI_CLASS_PATTERN = "^(hex{6})$".replace("hex", "[\da-fA-F]")
_PCI_ADDRESS_PATTERN = ("^(hex{4}):(hex{2}):(hex{2}).(oct{1})$".
                                             replace("hex", "[\da-fA-F]").
                                             replace("oct", "[0-7]"))
_PCI_ADDRESS_REGEX = re.compile(_PCI_ADDRESS_PATTERN)

_SRIOV_TOTALVFS = "sriov_totalvfs"


def pci_device_prop_match(pci_dev, specs):
    """Check if the pci_dev meet spec requirement

    Specs is a list of PCI device property requirements.
    An example of device requirement that the PCI should be either:
    a) Device with vendor_id as 0x8086 and product_id as 0x8259, or
    b) Device with vendor_id as 0x10de and product_id as 0x10d8:

    [{"vendor_id":"8086", "product_id":"8259"},
     {"vendor_id":"10de", "product_id":"10d8",
      "capabilities_network": ["rx", "tx", "tso", "gso"]}]

    """
    def _matching_devices(spec):
        for k, v in spec.items():
            pci_dev_v = pci_dev.get(k)
            if isinstance(v, list) and isinstance(pci_dev_v, list):
                if not all(x in pci_dev.get(k) for x in v):
                    # WRS: check for multi provider net:
                    if k == 'physical_network':
                        if (not pci_dev_v or
                            v not in pci_dev_v.replace(" ", "").split(',')):
                            return False
                    else:
                        return False
            elif pci_dev_v != v:
                # WRS: check for multi provider net:
                if k == 'physical_network':
                    if (not pci_dev_v or
                        v not in pci_dev_v.replace(" ", "").split(',')):
                        return False
                else:
                    return False
        return True

    return any(_matching_devices(spec) for spec in specs)


def parse_address(address):
    """Returns (domain, bus, slot, function) from PCI address that is stored in
    PciDevice DB table.
    """
    m = _PCI_ADDRESS_REGEX.match(address)
    if not m:
        raise exception.PciDeviceWrongAddressFormat(address=address)
    return m.groups()


def get_pci_address_fields(pci_addr):
    dbs, sep, func = pci_addr.partition('.')
    domain, bus, slot = dbs.split(':')
    return (domain, bus, slot, func)


def get_pci_address(domain, bus, slot, func):
    return '%s:%s:%s.%s' % (domain, bus, slot, func)


def get_function_by_ifname(ifname):
    """Given the device name, returns the PCI address of a device
    and returns True if the address is in a physical function.
    """
    dev_path = "/sys/class/net/%s/device" % ifname
    sriov_totalvfs = 0
    if os.path.isdir(dev_path):
        try:
            # sriov_totalvfs contains the maximum possible VFs for this PF
            with open(os.path.join(dev_path, _SRIOV_TOTALVFS)) as fd:
                sriov_totalvfs = int(fd.read())
                return (os.readlink(dev_path).strip("./"),
                        sriov_totalvfs > 0)
        except (IOError, ValueError):
            return os.readlink(dev_path).strip("./"), False
    return None, False


def is_physical_function(domain, bus, slot, function):
    dev_path = "/sys/bus/pci/devices/%(d)s:%(b)s:%(s)s.%(f)s/" % {
        "d": domain, "b": bus, "s": slot, "f": function}
    if os.path.isdir(dev_path):
        sriov_totalvfs = 0
        try:
            with open(dev_path + _SRIOV_TOTALVFS) as fd:
                sriov_totalvfs = int(fd.read())
                return sriov_totalvfs > 0
        except (IOError, ValueError):
            pass
    return False


def get_irqs_by_pci_address(pci_addr):
    """Get list of PCI IRQs based on a VF's pci address

    Raises PciDeviceNotFoundById in case the pci device is not found,
    or when there is an underlying problem getting associated irqs.
    :param pci_addr: PCI address
    :return: irqs, msi_irqs
    """
    irqs = set()
    msi_irqs = set()

    dev_path = "/sys/bus/pci/devices/%s" % (pci_addr)
    if not os.path.isdir(dev_path):
        raise exception.PciDeviceNotFoundById(id=pci_addr)

    _irqs = set()
    irq_path = "/sys/bus/pci/devices/%s/irq" % (pci_addr)
    try:
        with open(irq_path) as f:
            _irqs.update([int(x) for x in f.readline().split() if int(x) > 0])
    except Exception as e:
        LOG.error('get_irqs_by_pci_address: '
                  'pci_addr=%(A)s: irq_path=%(P)s; error=%(E)s',
                  {'A': pci_addr, 'P': irq_path, 'E': e})
        raise exception.PciDeviceNotFoundById(id=pci_addr)

    _msi_irqs = set()
    msi_path = "/sys/bus/pci/devices/%s/msi_irqs" % (pci_addr)
    try:
        _msi_irqs.update([int(x) for x in os.listdir(msi_path) if int(x) > 0])
    except OSError as e:
        # msi_path disappears during configuration; do not treat
        # non-existance as fatal
        if e.errno == errno.ENOENT:
            return (irqs, msi_irqs)
        else:
            LOG.error('get_irqs_by_pci_address: '
                      'pci_addr=%(A)s: msi_path=%(P)s; error=%(E)s',
                      {'A': pci_addr, 'P': msi_path, 'E': e})
            raise exception.PciDeviceNotFoundById(id=pci_addr)
    except Exception as e:
        LOG.error('get_irqs_by_pci_address: '
                  'pci_addr=%(A)s: msi_path=%(P)s; error=%(E)s',
                  {'A': pci_addr, 'P': msi_path, 'E': e})
        raise exception.PciDeviceNotFoundById(id=pci_addr)

    # Return only configured irqs, ignore any that are missing.
    for irq in _irqs:
        irq_path = "/proc/irq/%s" % (irq)
        if os.path.isdir(irq_path):
            irqs.update([irq])
    for irq in _msi_irqs:
        irq_path = "/proc/irq/%s" % (irq)
        if os.path.isdir(irq_path):
            msi_irqs.update([irq])
    return (irqs, msi_irqs)


def get_pci_irqs_pinned_cpuset(flavor=None, numa_topology=None,
                               pci_numa_node=None):
    """Get pinned cpuset where pci irq are affined.

    :param flavor: flavor
    :param pci_numa_node: numa node of a specific PCI device
    :param numa_topology: instance numa topology
    :return: cpuset, cpulist
    """
    cpuset = set()
    cpulist = ''

    if numa_topology is None or pci_numa_node is None or pci_numa_node < 0:
        return (cpuset, cpulist)

    # Determine full affinity cpuset, but restrict to pci's numa node
    for cell in numa_topology.cells:
        if cell.id == pci_numa_node:
            if cell.cpu_pinning is not None:
                cpuset.update(set(cell.cpu_pinning.values()))

    # Use extra-spec hw:pci_irq_affinity_mask only when the instance is pinned.
    if cpuset:
        pci_cpuset = hardware._get_pci_affinity_mask(flavor)
        if pci_cpuset:
            cpuset = set()
            for cell in numa_topology.cells:
                if cell.cpu_pinning is not None:
                    for vcpu in cell.cpuset:
                        if vcpu in pci_cpuset:
                            vcpu_cell, pcpu = numa_topology.vcpu_to_pcpu(vcpu)
                            cpuset.update(set([pcpu]))

    cpulist = utils.list_to_range(input_list=list(cpuset))
    return (cpuset, cpulist)


def set_irqs_affinity_by_pci_address(pci_addr, flavor=None,
                                     numa_topology=None):
    """Set cpu affinity for list of PCI IRQs with a VF's pci address,
    but restrict cpuset to the numa node of the PCI.

    Return list
    Raises PciDeviceNotFoundById in case the pci device is not found,
    or when there is an underlying problem getting associated irqs.
    :param pci_addr: PCI address
    :param flavor: flavor
    :param numa_topology: instance numa topology
    :return: irqs, msi_irqs, numa_node, cpulist
    """
    irqs = set()
    msi_irqs = set()
    numa_node = None
    cpulist = ''

    if numa_topology is None:
        return (irqs, msi_irqs, numa_node, cpulist)

    # Get the irqs associated with pci addr
    _irqs, _msi_irqs = get_irqs_by_pci_address(pci_addr)

    # Obtain physical numa_node for this pci addr
    numa_path = "/sys/bus/pci/devices/%s/numa_node" % (pci_addr)
    try:
        with open(numa_path) as f:
            numa_node = [int(x) for x in f.readline().split()][0]
    except Exception as e:
        LOG.error('set_irqs_affinity_by_pci_address: '
                  'pci_addr=%(A)s: numa_path=%(P)s; error=%(E)s',
                  {'A': pci_addr, 'P': numa_path, 'E': e})
        raise exception.PciDeviceNotFoundById(id=pci_addr)

    # Skip irq configuration if there is no associated numa node
    if numa_node is None or numa_node < 0:
        return (irqs, msi_irqs, numa_node, cpulist)

    # Determine the pinned cpuset where irqs are to be affined
    cpuset, cpulist = get_pci_irqs_pinned_cpuset(flavor=flavor,
                                                 numa_topology=numa_topology,
                                                 pci_numa_node=numa_node)

    # Skip irq configuration if there are no pinned cpus
    if not cpuset:
        return (irqs, msi_irqs, numa_node, cpulist)

    # Set IRQ affinity, but do not treat errors as fatal.
    for irq in _irqs:
        irq_aff_path = "/proc/irq/%s/smp_affinity_list" % (irq)
        try:
            with open(irq_aff_path, 'w') as f:
                f.write(cpulist)
            irqs.update([irq])
        except Exception as e:
            LOG.warning("Could not affine pci_addr:%(A)s, irq:%(I)s, "
                        "error=%(E)s",
                        {"A": pci_addr, "I": irq, "E": e})
    for irq in _msi_irqs:
        irq_aff_path = "/proc/irq/%s/smp_affinity_list" % (irq)
        try:
            with open(irq_aff_path, 'w') as f:
                f.write(cpulist)
            msi_irqs.update([irq])
        except Exception as e:
            LOG.warning("Could not affine pci_addr:%(A)s, irq:%(I)s, "
                        "error=%(E)s",
                        {"A": pci_addr, "I": irq, "E": e})
    return (irqs, msi_irqs, numa_node, cpulist)


def format_instance_pci_devices(pci_devices=None,
                                delim='\n'):
    """Returns formated pci devices list.

    :param pci_devices: `nova.objects.PciDeviceList` object
    """
    if pci_devices is None:
        return ''

    devs = []
    for pci_dev in pci_devices:
        str = ('node:%s, addr:%s, type:%s, vendor:%s, product:%s'
               % (pci_dev.numa_node,
                  pci_dev.address,
                  pci_dev.dev_type.replace('type-', ''),
                  pci_dev.vendor_id,
                  pci_dev.product_id
                  ))
        devs.append(str)

    return '%s' % (delim.join(devs))


def _get_sysfs_netdev_path(pci_addr, pf_interface):
    """Get the sysfs path based on the PCI address of the device.

    Assumes a networking device - will not check for the existence of the path.
    """
    if pf_interface:
        return "/sys/bus/pci/devices/%s/physfn/net" % (pci_addr)
    return "/sys/bus/pci/devices/%s/net" % (pci_addr)


def get_ifname_by_pci_address(pci_addr, pf_interface=False):
    """Get the interface name based on a VF's pci address

    The returned interface name is either the parent PF's or that of the VF
    itself based on the argument of pf_interface.
    """
    dev_path = _get_sysfs_netdev_path(pci_addr, pf_interface)
    try:
        dev_info = os.listdir(dev_path)
        return dev_info.pop()
    except Exception:
        raise exception.PciDeviceNotFoundById(id=pci_addr)


def get_mac_by_pci_address(pci_addr, pf_interface=False):
    """Get the MAC address of the nic based on it's PCI address

    Raises PciDeviceNotFoundById in case the pci device is not a NIC
    """
    dev_path = _get_sysfs_netdev_path(pci_addr, pf_interface)
    if_name = get_ifname_by_pci_address(pci_addr, pf_interface)
    addr_file = os.path.join(dev_path, if_name, 'address')

    try:
        with open(addr_file) as f:
            mac = next(f).strip()
            return mac
    except (IOError, StopIteration) as e:
        LOG.warning("Could not find the expected sysfs file for "
                    "determining the MAC address of the PCI device "
                    "%(addr)s. May not be a NIC. Error: %(e)s",
                    {'addr': pci_addr, 'e': e})
        raise exception.PciDeviceNotFoundById(id=pci_addr)


def get_vf_num_by_pci_address(pci_addr):
    """Get the VF number based on a VF's pci address

    A VF is associated with an VF number, which ip link command uses to
    configure it. This number can be obtained from the PCI device filesystem.
    """
    VIRTFN_RE = re.compile("virtfn(\d+)")
    virtfns_path = "/sys/bus/pci/devices/%s/physfn/virtfn*" % (pci_addr)
    vf_num = None
    try:
        for vf_path in glob.iglob(virtfns_path):
            if re.search(pci_addr, os.readlink(vf_path)):
                t = VIRTFN_RE.search(vf_path)
                vf_num = t.group(1)
                break
    except Exception:
        pass
    if vf_num is None:
        raise exception.PciDeviceNotFoundById(id=pci_addr)
    return vf_num


def get_net_name_by_vf_pci_address(vfaddress):
    """Given the VF PCI address, returns the net device name.

    Every VF is associated to a PCI network device. This function
    returns the libvirt name given to this network device; e.g.:

        <device>
            <name>net_enp8s0f0_90_e2_ba_5e_a6_40</name>
        ...

    In the libvirt parser information tree, the network device stores the
    network capabilities associated to this device.
    """
    try:
        mac = get_mac_by_pci_address(vfaddress).split(':')
        ifname = get_ifname_by_pci_address(vfaddress)
        return ("net_%(ifname)s_%(mac)s" %
                {'ifname': ifname, 'mac': '_'.join(mac)})
    except Exception:
        LOG.warning("No net device was found for VF %(vfaddress)s",
                    {'vfaddress': vfaddress})
        return


def vif_model_pci_passthrough(vif_model):
    """Checks whether the supplied VIF model is a pci-passthrough
    device or pci-sriov
    :param vif_model: The VIF model to check
    :return: TRUE if pci-passthrough VIF model, otherwise FALSE
    """
    # TODO(mpeters): support other (more specific) PCI passthrough models
    return vif_model in [network_model.VIF_MODEL_PCI_SRIOV,
                         network_model.VIF_MODEL_PCI_PASSTHROUGH]
