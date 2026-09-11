from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw

from aftersales_workbench.workflows.desktop_sender import (
    DesktopAmbiguousSendError,
    DesktopBeforePasteError,
)
from aftersales_workbench.workflows.wecom_receipt import (
    ReceiptObservation,
    WeComReceiptReader,
    group_key,
    message_key,
)
from aftersales_workbench.workflows.windows_wecom import WindowsWeComGateway

GROUP = '测试-快递群'
MESSAGE = '【售后快递拦截】\n发货运单号：JT12345678\n处理要求：请拦截退回。'


def scene(*, bubble=True, draft=False, mark=False, incoming=False):
    image = Image.new('RGB', (1400, 857), (246, 247, 251))
    draw = ImageDraw.Draw(image)
    draw.rectangle((360, 624, 1200, 837), fill='white')
    if bubble:
        draw.rectangle((714, 394, 1199, 484), fill=(225, 228, 232) if incoming
                       else (198, 230, 253))
        draw.rectangle((735, 410, 850, 424), fill='black')
    if draft:
        draw.rectangle((375, 685, 800, 700), fill='black')
    else:
        draw.line((375, 685, 375, 705), fill='black')
    if mark:
        draw.ellipse((690, 430, 705, 445), fill=(230, 40, 40))
    return image


class FakeOcr:
    def __init__(self, *, title=GROUP, text=MESSAGE):
        self.title = title
        self.text = text

    def read(self, image):
        return self.title if image.height < 60 else self.text


def test_full_outgoing_message_with_blank_draft_is_visible():
    result = WeComReceiptReader(FakeOcr()).inspect(scene(), GROUP, MESSAGE)
    assert result.sent_visible
    assert result.matching_bubbles == 1


@pytest.mark.parametrize('changes', [
    {'draft': True}, {'mark': True}, {'incoming': True}, {'bubble': False},
])
def test_draft_failure_marker_incoming_reply_and_missing_bubble_are_not_success(changes):
    result = WeComReceiptReader(FakeOcr()).inspect(scene(**changes), GROUP, MESSAGE)
    assert not result.sent_visible


@pytest.mark.parametrize('title,text', [
    ('其他快递群', MESSAGE), (GROUP, MESSAGE.replace('12345678', '12345679')),
    (GROUP, MESSAGE.replace('拦截退回', '不要退回')), (GROUP, 'JT12345678已登记'),
])
def test_wrong_group_tracking_or_body_never_match(title, text):
    result = WeComReceiptReader(FakeOcr(title=title, text=text)).inspect(scene(), GROUP, MESSAGE)
    assert not result.sent_visible


def test_two_identical_bubbles_are_ambiguous():
    image = scene()
    image.paste(image.crop((714, 394, 1200, 485)), (714, 200))
    result = WeComReceiptReader(FakeOcr()).inspect(image, GROUP, MESSAGE)
    assert result.matching_bubbles == 2
    assert not result.sent_visible


def test_waiting_spinner_is_not_success():
    image = scene()
    ImageDraw.Draw(image).arc((690, 430, 705, 445), 20, 280, fill=(180, 180, 180), width=2)
    assert not WeComReceiptReader(FakeOcr()).inspect(image, GROUP, MESSAGE).sent_visible


def test_normalization_never_fuzzes_identifiers_or_chinese_words():
    assert message_key('【运单】 JT123') == message_key('[运 单]JT123')
    assert message_key('JT123') != message_key('JT128')
    assert message_key('请退回') != message_key('请退货')
    assert group_key('测试·快递群') == group_key(GROUP)
    assert group_key('测试二-快递群') != group_key(GROUP)


@pytest.mark.parametrize('always_clear', [True, False])
def test_receipt_requires_three_observations_over_two_seconds(monkeypatch, always_clear):
    from aftersales_workbench.workflows import windows_wecom as module

    gateway = object.__new__(WindowsWeComGateway)
    now = [0.0]
    times = []
    monkeypatch.setattr(module.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(gateway, '_sleep_range', lambda *args, **kwargs:
                        now.__setitem__(0, now[0] + .5))

    def observe(*args, **kwargs):
        times.append(now[0])
        clear = always_clear or len(times) % 2 == 1
        return ReceiptObservation(True, True, False, 1, clear)

    monkeypatch.setattr(gateway, '_read_receipt', observe)
    if always_clear:
        gateway._wait_for_receipt(11, None)
        assert len(times) >= 3 and times[-1] - times[0] >= 2
    else:
        with pytest.raises(DesktopAmbiguousSendError, match='连续核验'):
            gateway._wait_for_receipt(11, None)


def test_ocr_error_after_send_stays_ambiguous(monkeypatch):
    gateway = object.__new__(WindowsWeComGateway)
    monkeypatch.setattr(gateway, '_raise_if_escape', lambda **kwargs: None)
    monkeypatch.setattr(gateway, '_full_snapshot', lambda *args, **kwargs: scene())

    def broken(*args):
        raise RuntimeError('OCR unavailable')

    gateway.receipt_reader = SimpleNamespace(inspect=broken)
    with pytest.raises(DesktopAmbiguousSendError, match='未确认发送成功'):
        gateway._read_receipt(
            11, SimpleNamespace(target_group=GROUP, message=MESSAGE), ambiguous=True,
        )


@pytest.mark.parametrize('group,empty,draft,expected', [
    (False, True, True, []), (True, False, True, []), (True, True, False, ['paste']),
])
def test_bad_group_existing_draft_or_wrong_typed_text_never_press_send(
    monkeypatch, group, empty, draft, expected,
):
    gateway = object.__new__(WindowsWeComGateway)
    gateway.user32 = SimpleNamespace(GetForegroundWindow=lambda: 99)
    for name in ['_raise_if_security_window', '_raise_if_escape', '_hotkey', '_tap',
                 '_sleep_range', '_snapshot', '_wait_for_change', '_type_unicode',
                 '_type_multiline_message', '_restore_previous_window']:
        monkeypatch.setattr(gateway, name, lambda *args, **kwargs: None)
    monkeypatch.setattr(gateway, '_activate_wecom_foreground', lambda: (11, 101))
    monkeypatch.setattr(gateway, '_require_wecom_foreground', lambda **kwargs: (11, 101))
    monkeypatch.setattr(gateway, '_read_receipt', lambda *args, **kwargs: SimpleNamespace(
        group_matches=group, input_empty=empty, draft_matches=draft, matching_bubbles=0,
    ))
    events = []
    hooks = SimpleNamespace(paste_started=lambda: events.append('paste'),
                            send_pressed=lambda: events.append('send'),
                            sent=lambda: events.append('sent'))
    with pytest.raises((DesktopBeforePasteError, DesktopAmbiguousSendError)):
        gateway.send(SimpleNamespace(target_group=GROUP, message=MESSAGE), hooks)
    assert events == expected
