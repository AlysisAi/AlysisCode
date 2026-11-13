"""Build a bounded private-pilot ARM template; never deploy on import.

The trusted controller is separate from agent computers. Only the operator's
single IPv4 address can reach SSH; the bot API and PostgreSQL are not exposed.
Keys are supplied by the operator in RAM. Persistent disks and storage survive
shutdown. This is a small pilot, not an HA/production deployment template.
"""

import re
from datetime import UTC, datetime
from ipaddress import IPv4Address


def template(
    *,
    name,
    storage_name,
    location,
    vm_size,
    operator_ip,
    ssh_public_key,
    image_version,
    cutoff,
    shutdown_location=None,
):
    if not re.fullmatch(r"[a-z][a-z0-9-]{2,40}", name):
        raise ValueError("Invalid pilot resource prefix")
    if not re.fullmatch(r"[a-z0-9]{3,24}", storage_name):
        raise ValueError("Invalid storage name")
    if not re.fullmatch(r"[a-z0-9]{1,40}", location):
        raise ValueError("Use an Azure region identifier")
    shutdown_location = shutdown_location or location
    if shutdown_location != location:
        raise ValueError(
            "Native VM shutdown must use the VM region; qualify another guard separately"
        )
    if not re.fullmatch(r"Standard_[A-Za-z0-9_]+", vm_size):
        raise ValueError("Use an explicitly qualified VM size")
    address = IPv4Address(operator_ip)
    if not address.is_global:
        raise ValueError("Use the operator's single public IPv4 address")
    if not ssh_public_key.startswith(("ssh-ed25519 ", "ssh-rsa ")):
        raise ValueError("Provide an SSH public key; private keys are not accepted")
    if cutoff.tzinfo is None or not 300 < (cutoff - datetime.now(UTC)).total_seconds() <= 21600:
        raise ValueError("Pilot shutdown must be between five minutes and six hours away")
    if not re.fullmatch(r"[0-9.]+", image_version):
        raise ValueError("Pin the observed OS image version")
    cutoff = cutoff.astimezone(UTC)
    tags = {"purpose": "alysis-bots-private-pilot", "shutdown-by": cutoff.isoformat()}

    def rid(kind, suffix=""):
        return f"[resourceId('{kind}', '{name}{suffix}')]"

    resources = []

    def add(resource_type, resource_name, version, properties, *, depends=(), **extra):
        resources.append(
            {
                "type": resource_type,
                "apiVersion": version,
                "name": resource_name,
                "location": location,
                "tags": tags,
                "dependsOn": list(depends),
                "properties": properties,
                **extra,
            }
        )

    add(
        "Microsoft.Network/networkSecurityGroups",
        name,
        "2024-05-01",
        {
            "securityRules": [
                {
                    "name": "OperatorSSH",
                    "properties": {
                        "priority": 100,
                        "direction": "Inbound",
                        "access": "Allow",
                        "protocol": "Tcp",
                        "sourceAddressPrefix": str(address) + "/32",
                        "sourcePortRange": "*",
                        "destinationAddressPrefix": "*",
                        "destinationPortRange": "22",
                    },
                },
                {
                    "name": "DenyOtherInbound",
                    "properties": {
                        "priority": 4096,
                        "direction": "Inbound",
                        "access": "Deny",
                        "protocol": "*",
                        "sourceAddressPrefix": "*",
                        "sourcePortRange": "*",
                        "destinationAddressPrefix": "*",
                        "destinationPortRange": "*",
                    },
                },
            ],
        },
    )
    add(
        "Microsoft.Network/virtualNetworks",
        name,
        "2024-05-01",
        {
            "addressSpace": {"addressPrefixes": ["10.87.0.0/24"]},
            "subnets": [
                {
                    "name": "control",
                    "properties": {
                        "addressPrefix": "10.87.0.0/26",
                        "defaultOutboundAccess": False,
                        "networkSecurityGroup": {
                            "id": rid("Microsoft.Network/networkSecurityGroups")
                        },
                        "serviceEndpoints": [{"service": "Microsoft.Storage", "locations": ["*"]}],
                    },
                }
            ],
        },
        depends=[rid("Microsoft.Network/networkSecurityGroups")],
    )
    subnet = f"[resourceId('Microsoft.Network/virtualNetworks/subnets', '{name}', 'control')]"
    add(
        "Microsoft.Network/publicIPAddresses",
        name,
        "2024-05-01",
        {
            "publicIPAllocationMethod": "Static",
            "publicIPAddressVersion": "IPv4",
        },
        sku={"name": "Standard"},
    )
    add(
        "Microsoft.Network/networkInterfaces",
        name,
        "2024-05-01",
        {
            "ipConfigurations": [
                {
                    "name": "control",
                    "properties": {
                        "privateIPAllocationMethod": "Dynamic",
                        "subnet": {"id": subnet},
                        "publicIPAddress": {"id": rid("Microsoft.Network/publicIPAddresses")},
                    },
                }
            ],
            "enableIPForwarding": False,
        },
        depends=[
            rid("Microsoft.Network/virtualNetworks"),
            rid("Microsoft.Network/publicIPAddresses"),
        ],
    )
    add(
        "Microsoft.Compute/virtualMachines",
        name,
        "2024-11-01",
        {
            "hardwareProfile": {"vmSize": vm_size},
            "osProfile": {
                "computerName": name,
                "adminUsername": "alysisoperator",
                "linuxConfiguration": {
                    "disablePasswordAuthentication": True,
                    "provisionVMAgent": True,
                    "ssh": {
                        "publicKeys": [
                            {
                                "path": "/home/alysisoperator/.ssh/authorized_keys",
                                "keyData": ssh_public_key,
                            }
                        ]
                    },
                },
            },
            "storageProfile": {
                "imageReference": {
                    "publisher": "Canonical",
                    "offer": "ubuntu-24_04-lts",
                    "sku": "server",
                    "version": image_version,
                },
                "osDisk": {
                    "createOption": "FromImage",
                    "deleteOption": "Detach",
                    "managedDisk": {"storageAccountType": "StandardSSD_LRS"},
                },
                "dataDisks": [
                    {
                        "lun": 0,
                        "createOption": "Empty",
                        "diskSizeGB": 32,
                        "deleteOption": "Detach",
                        "caching": "None",
                        "managedDisk": {"storageAccountType": "StandardSSD_LRS"},
                    }
                ],
            },
            "networkProfile": {
                "networkInterfaces": [
                    {
                        "id": rid("Microsoft.Network/networkInterfaces"),
                        "properties": {"deleteOption": "Detach"},
                    }
                ]
            },
            "securityProfile": {
                "securityType": "TrustedLaunch",
                "uefiSettings": {"secureBootEnabled": True, "vTpmEnabled": True},
            },
        },
        depends=[rid("Microsoft.Network/networkInterfaces")],
        identity={"type": "SystemAssigned"},
    )
    add(
        "Microsoft.DevTestLab/schedules",
        "shutdown-computevm-" + name,
        "2018-09-15",
        {
            "status": "Enabled",
            "taskType": "ComputeVmShutdownTask",
            "dailyRecurrence": {"time": cutoff.strftime("%H%M")},
            "timeZoneId": "UTC",
            "targetResourceId": rid("Microsoft.Compute/virtualMachines"),
            "notificationSettings": {"status": "Disabled", "timeInMinutes": 30},
        },
        depends=[rid("Microsoft.Compute/virtualMachines")],
        location=shutdown_location,
    )
    add(
        "Microsoft.Storage/storageAccounts",
        storage_name,
        "2023-05-01",
        {
            "minimumTlsVersion": "TLS1_2",
            "supportsHttpsTrafficOnly": True,
            "allowBlobPublicAccess": False,
            "allowSharedKeyAccess": False,
            "publicNetworkAccess": "Enabled",
            "networkAcls": {
                "defaultAction": "Deny",
                "bypass": "None",
                "ipRules": [],
                "virtualNetworkRules": [{"id": subnet, "action": "Allow"}],
            },
        },
        depends=[rid("Microsoft.Network/virtualNetworks")],
        sku={"name": "Standard_LRS"},
        kind="StorageV2",
    )
    # Child storage resources have no region/tags field.
    resources.append(
        {
            "type": "Microsoft.Storage/storageAccounts/blobServices/containers",
            "apiVersion": "2023-05-01",
            "name": storage_name + "/default/bots",
            "dependsOn": [f"[resourceId('Microsoft.Storage/storageAccounts', '{storage_name}')]"],
            "properties": {"publicAccess": "None"},
        }
    )
    return {
        "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#",
        "contentVersion": "1.0.0.0",
        "resources": resources,
    }
