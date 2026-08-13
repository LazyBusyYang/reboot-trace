from pathlib import Path
import re
import subprocess


ROOT=Path(__file__).resolve().parents[2]
SCRIPTS=ROOT/"scripts/instances"


def test_instance_wrappers_map_to_dedicated_instances():
    for instance in (1,4,5,9):
        path=SCRIPTS/f"start-dev-{instance}.sh"
        text=path.read_text()
        assert f'reboot-trace-instance.sh" {instance} ' in text
        subprocess.run(["bash","-n",str(path)],check=True)


def test_common_script_enforces_deployment_contract():
    path=SCRIPTS/"reboot-trace-instance.sh"
    text=path.read_text()
    subprocess.run(["bash","-n",str(path)],check=True)
    assert "runtime/dev-" not in text  # path is composed from runtime_root and instance
    assert "instance_root=$runtime_root/dev-$instance" in text
    assert "marker_dir=/tmp/reboot-trace-$UID" in text
    assert "chmod 700 \"$marker_dir\"" in text
    assert "RT_REQUIRE_CONTAINER_MARKER=true" in text
    assert "RT_IDENTITY_SCOPE=local_container" in text
    assert "RT_INSTANCE_MARKER_PATH=\"$marker_path\"" in text
    assert "validate-http" in text
    assert "RT_SERVICE_TOKEN" in text
    assert "process_control terminate" in text
    assert "terminate-legacy" in text
    assert "process state is not trustworthy" in text
    assert re.search(r"port=\$\{REBOOT_TRACE_PORT:-31088\}",text)
    assert "https://maoshanwang-reboot-trace.zoedev.top" in text
