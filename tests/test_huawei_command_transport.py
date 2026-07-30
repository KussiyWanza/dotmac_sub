from app.services.network.olt_ssh import _send_huawei_command


def test_send_huawei_command_preserves_spaces_in_one_line() -> None:
    sent: list[str] = []

    class FakeChannel:
        def send(self, data: str) -> None:
            sent.append(data)

    _send_huawei_command(FakeChannel(), "display traffic table ip from-index 0")

    assert sent == ["display traffic table ip from-index 0\n"]
