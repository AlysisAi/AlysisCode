from datetime import UTC, datetime, timedelta

import pytest

from scripts.qa.bots_hosted_infrastructure import template


def config():
    return dict(
        name="alysis-synthetic-pilot",
        storage_name="alysissyntheticpilot",
        location="northeurope",
        vm_size="Standard_D2as_v5",
        operator_ip="8.8.8.8",
        ssh_public_key="ssh-rsa synthetic-public-key",
        image_version="24.04.202610160",
        cutoff=datetime.now(UTC) + timedelta(hours=5),
    )


def test_pilot_ingress_storage_and_shutdown_boundaries():
    resources = {r["type"]: r for r in template(**config())["resources"]}
    rules = resources["Microsoft.Network/networkSecurityGroups"]["properties"]["securityRules"]
    allows = [r["properties"] for r in rules if r["properties"]["access"] == "Allow"]
    assert len(allows) == 1
    assert allows[0]["sourceAddressPrefix"] == "8.8.8.8/32"
    assert allows[0]["destinationPortRange"] == "22"
    storage = resources["Microsoft.Storage/storageAccounts"]["properties"]
    assert storage["networkAcls"]["defaultAction"] == "Deny"
    assert storage["allowSharedKeyAccess"] is False
    assert storage["allowBlobPublicAccess"] is False
    shutdown = resources["Microsoft.DevTestLab/schedules"]["properties"]
    assert shutdown["status"] == "Enabled"
    assert shutdown["taskType"] == "ComputeVmShutdownTask"
    assert shutdown["notificationSettings"]["status"] == "Disabled"
    vm = resources["Microsoft.Compute/virtualMachines"]["properties"]
    assert vm["osProfile"]["linuxConfiguration"]["disablePasswordAuthentication"]
    assert vm["storageProfile"]["osDisk"]["deleteOption"] == "Detach"
    assert vm["storageProfile"]["dataDisks"][0]["deleteOption"] == "Detach"


def test_cross_region_native_shutdown_rejected_before_deployment():
    with pytest.raises(ValueError, match="VM region"):
        template(**config(), shutdown_location="spaincentral")


@pytest.mark.parametrize("ip", ["0.0.0.0/0", "8.8.8.8/24", "127.0.0.1", "10.1.2.3"])
def test_no_network_expansion(ip):
    values = config()
    values["operator_ip"] = ip
    with pytest.raises(ValueError):
        template(**values)


@pytest.mark.parametrize("delta", [timedelta(seconds=-1), timedelta(days=1)])
def test_no_expired_or_unbounded_shutdown(delta):
    values = config()
    values["cutoff"] = datetime.now(UTC) + delta
    with pytest.raises(ValueError, match="shutdown"):
        template(**values)
