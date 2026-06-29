"""Test per il modulo safety — comandi pericolosi, safe, critici."""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.safety import SafetyGuard, ActionRisk


def test_critical_commands():
    """Comandi critici devono essere bloccati."""
    sg = SafetyGuard()
    critical = [
        "rm -rf /",
        "rm -rf /home",
        "dd if=/dev/zero of=/dev/sda",
        "mkfs.ext4 /dev/sda1",
        ":(){ :|:& };:",
        "chmod -R 777 /",
        "shutdown -h now",
        "git push --force main",
    ]
    for cmd in critical:
        risk = sg.classify(cmd)
        assert risk == ActionRisk.CRITICAL, f"{cmd} dovrebbe essere CRITICAL, ma e {risk}"


def test_approval_commands():
    """Comandi che richiedono approvazione."""
    sg = SafetyGuard()
    approval = [
        "sudo apt update",
        "apt install vim",
        "pip install requests",
        "git push origin main",
        "git reset --hard HEAD",
        "rm file.txt",
        "rm -r old_dir",
        "rmdir old_folder",
        "kill 1234",
        "killall python",
        "git branch -D feature",
        "git clean -fd",
    ]
    for cmd in approval:
        risk = sg.classify(cmd)
        assert risk == ActionRisk.HIGH, f"{cmd} dovrebbe essere HIGH, ma e {risk}"

    # mv è lavoro quotidiano: niente approvazione (i path concordati
    # sono protetti dai patti), ma resta MODERATE, non SAFE
    assert sg.classify("mv file.txt /tmp/") == ActionRisk.MODERATE


def test_safe_commands():
    """Comandi sicuri devono passare liberamente."""
    sg = SafetyGuard()
    safe = [
        "ls -la",
        "cat file.txt",
        "grep pattern file.py",
        "git status",
        "git log --oneline",
        "git diff",
        "pwd",
        "whoami",
        "date",
        "python3 -c 'print(1)'",
        "ps aux",
    ]
    for cmd in safe:
        risk = sg.classify(cmd)
        assert risk == ActionRisk.SAFE, f"{cmd} dovrebbe essere SAFE, ma e {risk}"


def test_critical_file_detection():
    """Deve rilevare modifiche a file critici."""
    sg = SafetyGuard(openvurp_dir="/fake/openvurp")
    assert sg.is_critical_file("/fake/openvurp/agent.py")
    assert sg.is_critical_file("/fake/openvurp/main.py")
    assert sg.is_critical_file("/fake/openvurp/config.py")
    assert not sg.is_critical_file("/fake/openvurp/soul.md")


if __name__ == "__main__":
    test_critical_commands()
    test_approval_commands()
    test_safe_commands()
    test_critical_file_detection()
    print("Tutti i test safety passati!")
