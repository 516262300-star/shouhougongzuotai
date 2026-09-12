import pytest
from PIL import Image, ImageDraw

from aftersales_workbench.workflows import windows_wecom as module
from aftersales_workbench.workflows.desktop_sender import (
    DesktopBeforePasteError,
    DesktopForegroundUnavailableError,
    DesktopSearchUnavailableError,
)
from aftersales_workbench.workflows.wecom_search import search_field_focused


def scene(focused=True, offset=0):
    image = Image.new('RGB', (1400 + offset * 2, 857 + offset * 2), (246, 247, 251))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((86 + offset, 25 + offset, 277 + offset, 60 + offset),
                           radius=6, fill='white', outline=(53, 123, 208) if focused else 'gray')
    return image


@pytest.mark.parametrize('offset,scale', [(0, 1), (20, 1), (0, 2), (20, 2)])
def test_focus_detection_handles_border_and_display_scale(offset, scale):
    for focused in (True, False):
        image = scene(focused, offset)
        image = image.resize((image.width * scale, image.height * scale))
        assert search_field_focused(image) is focused


def test_unrelated_blue_selection_and_partial_frame_are_not_search_focus():
    image = scene(False)
    draw = ImageDraw.Draw(image)
    draw.rectangle((90, 100, 330, 155), fill=(53, 123, 208))
    assert not search_field_focused(image)
    image = scene()
    ImageDraw.Draw(image).rectangle((80, 48, 280, 65), fill='white')
    assert not search_field_focused(image)
    assert not search_field_focused(Image.new('RGB', (640, 480)))


def search_gateway(monkeypatch, images):
    gateway = object.__new__(module.WindowsWeComGateway)
    now, keys = [0.0], []
    monkeypatch.setattr(module.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(gateway, '_sleep_range', lambda *args:
                        now.__setitem__(0, now[0] + .2))
    monkeypatch.setattr(gateway, '_hotkey', lambda *keys_sent: keys.append(keys_sent))
    for name in ('_raise_if_escape', '_raise_if_security_window', '_require_target_foreground'):
        monkeypatch.setattr(gateway, name, lambda *args, **kwargs: None)
    monkeypatch.setattr(gateway, '_full_snapshot', lambda *args: images())
    return gateway, keys, now


def test_already_focused_search_continues_without_requiring_image_change(monkeypatch):
    gateway, keys, now = search_gateway(monkeypatch, lambda: scene())
    gateway._open_group_search(11, 101)
    assert keys == [(module.VK_CONTROL, module.VK_F)]
    assert now[0] == 0


def test_delayed_focus_retries_shortcut_before_any_text(monkeypatch):
    images = iter([scene(False)] * 12 + [scene()])
    gateway, keys, _ = search_gateway(monkeypatch, lambda: next(images))
    gateway._open_group_search(11, 101)
    assert keys == [(module.VK_CONTROL, module.VK_F)] * 2


def test_search_timeout_is_retryable_and_never_types_or_sends(monkeypatch):
    gateway, keys, now = search_gateway(monkeypatch, lambda: scene(False))
    with pytest.raises(DesktopSearchUnavailableError):
        gateway._open_group_search(11, 101)
    assert keys == [(module.VK_CONTROL, module.VK_F)] * 2
    assert 4.4 <= now[0] < 5.0


@pytest.mark.parametrize('guard,error_type', [
    ('_raise_if_escape', DesktopBeforePasteError),
    ('_raise_if_security_window', DesktopBeforePasteError),
    ('_require_target_foreground', DesktopForegroundUnavailableError),
])
def test_search_preserves_escape_security_and_foreground_guards(monkeypatch, guard, error_type):
    gateway, keys, _ = search_gateway(monkeypatch, lambda: scene())

    def stop(*args, **kwargs):
        raise error_type('guard stopped')

    monkeypatch.setattr(gateway, guard, stop)
    with pytest.raises(error_type, match='guard stopped'):
        gateway._open_group_search(11, 101)
    assert len(keys) <= 1
