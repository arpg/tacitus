"""The web app in a browser: the drawer's width and a room's menu.

The channel list takes the width its names need and its edge drags or
keys another that the browser keeps, its foot stays inside it at every
width, and on a phone every control is 44px high and nothing scrolls
sideways. A room's menu (dots with a mouse, a long press with a finger)
renames it in place, archives it after one question, brings an archived
one back and mutes it; debug mode leaves the health room out.

The browser fixtures (one Chromium per file, a fresh context and stub per
test) and the helpers more than one file uses are in tests/webui_browser.py;
tests/test_webui.py holds the checks that need no browser.
"""
import re
import sys
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))

from webui_browser import (  # noqa: E402,F401  (the fixtures and helpers)
    FAKE_PROMPT, PHONE, SIDEWAYS, SMALL_TARGETS, SUBS, app, browser, drawer_drawn, open_thread,
    stub_factory, wait_for,
)


# -- the menu's width: its own, the owner's, and its foot at every width ----

NAV_W = "Math.round($('channels').getBoundingClientRect().width)"


KEPT_W = "localStorage.getItem('tacitus-menu-width')"


# The height of each name on show, and the hover text of each.
NAMES = """[...document.querySelectorAll('#channels .c-name')].filter((el) => el.offsetWidth)
    .map((el) => [el.textContent, el.offsetHeight, el.getAttribute('title')])"""


NO_STORAGE = "Object.defineProperty(window, 'localStorage', { get() { throw new DOMException('denied', 'SecurityError'); } });"


def _drag(page, dx):
    box = page.locator("#nav-edge").bounding_box()
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x + dx, y, steps=6)
    page.mouse.up()


def test_the_menu_takes_the_width_its_names_need_and_drags_wider(app):
    """Where the menu is a column, it is as wide as its longest name on one
    line, within 240 to 300px; a longer name wraps to a second line, and
    one cut short there shows whole on hover. Its edge is a separator: a
    drag sets the width, which this browser keeps over a reload, the
    conversation keeps 480px whatever the width, and a double-click gives
    the menu its own width again. Without storage it works the same for
    the visit."""
    from playwright.sync_api import expect
    page, stub, errors = app()
    page.wait_for_selector(".thread-item")
    edge = page.locator("#nav-edge")
    for k, v in {"role": "separator", "aria-orientation": "vertical", "aria-controls": "channels",
                 "aria-valuemin": "180", "aria-valuemax": "420", "tabindex": "0"}.items():
        expect(edge).to_have_attribute(k, v)
    expect(edge).to_be_visible()
    own = page.evaluate(NAV_W)
    assert 240 <= own <= 300
    expect(edge).to_have_attribute("aria-valuenow", str(own))
    names = page.evaluate(NAMES)
    assert {n for n, _h, _t in names} >= {"Action requests", "Ask Tacitus", "Scheduling", "example-project"}
    assert all(hgt < 25 and title is None for _n, hgt, title in names), names  # one line each, nothing cut
    # A long name wraps to two lines at the widest the menu takes by itself; a longer one is cut there.
    stub.add_room("made up workshop on rooms with long names")
    stub.add_room("an imaginary programme on the wrapping of very long room names in a narrow menu")
    expect(page.locator(".c-name", has_text="an-imaginary-programme")).to_have_count(1)
    wait_for(lambda: page.evaluate(NAV_W) == 300)
    names = {n: (hgt, title) for n, hgt, title in page.evaluate(NAMES)}
    hgt, title = names["made-up-workshop-on-rooms-with-long-names"]
    assert 30 < hgt < 45 and title is None
    long = "an-imaginary-programme-on-the-wrapping-of-very-long-room-names-in-a-narrow-menu"
    assert names[long][0] < 45 and names[long][1] == long
    # A drag sets the width; this browser keeps it.
    _drag(page, 60)
    assert page.evaluate(NAV_W) == 360
    expect(edge).to_have_attribute("aria-valuenow", "360")
    assert page.evaluate(KEPT_W) == "360"
    page.reload()
    page.wait_for_selector(".thread-item")
    assert page.evaluate(NAV_W) == 360
    # As wide as it goes: 420px, and on a narrower window the conversation keeps 480px.
    _drag(page, 300)
    assert page.evaluate(NAV_W) == 420
    assert page.evaluate("$('thread').getBoundingClientRect().width") == 1280 - 420 - 320
    page.set_viewport_size({"width": 1201, "height": 800})
    assert page.evaluate("$('thread').getBoundingClientRect().width") >= 480
    page.set_viewport_size({"width": 1280, "height": 800})
    assert page.evaluate(NAV_W) == 420
    # A double-click gives back the menu's own width, and forgets the one kept.
    edge.dblclick()
    wait_for(lambda: page.evaluate(NAV_W) == 300)
    assert page.evaluate(KEPT_W) is None
    assert not errors, errors
    # A browser that keeps nothing: the menu at its own width, and a drag for this visit.
    page, stub, errors = app(init=NO_STORAGE)
    page.wait_for_selector(".thread-item")
    page.click("#welcome-skip")  # it cannot remember the welcome was closed either
    own = page.evaluate(NAV_W)
    assert 240 <= own <= 300
    _drag(page, 40)
    assert page.evaluate(NAV_W) == own + 40
    assert not errors, errors


def test_the_menu_s_edge_moves_with_the_keys_and_keeps_the_split(app):
    """The separator takes the focus: the arrow keys move it 16px (64px with
    Shift), Home and End take it to 180 and 420px, and Enter gives the menu
    its own width again. In a window 1000 to 1200px wide, split at its
    centre, the menu takes from the conversations beside it, which keep
    300px at the menu's own width and 260px at the owner's."""
    from playwright.sync_api import expect
    page, stub, errors = app()
    page.wait_for_selector(".thread-item")
    edge = page.locator("#nav-edge")
    own = page.evaluate(NAV_W)
    edge.focus()
    for key, want in (("ArrowRight", own + 16), ("Shift+ArrowLeft", own - 48), ("Home", 180), ("End", 420)):
        edge.press(key)
        assert page.evaluate(NAV_W) == want, key
        expect(edge).to_have_attribute("aria-valuenow", str(want))
        assert page.evaluate(KEPT_W) == str(want)
    edge.press("Enter")
    assert page.evaluate(NAV_W) == own
    assert page.evaluate(KEPT_W) is None
    page.set_viewport_size({"width": 1100, "height": 800})
    split = "[$('threads').getBoundingClientRect().width, $('thread').getBoundingClientRect().left]"
    assert page.evaluate(NAV_W) == min(own, 250)
    assert page.evaluate(split) == [550 - min(own, 250), 550]
    edge.press("End")
    assert page.evaluate(NAV_W) == 290
    assert page.evaluate(split) == [260, 550]
    assert page.evaluate(KEPT_W) == "290"  # the width shown, not the one asked for
    assert not errors, errors


# The foot's controls on show that reach outside the menu.
OUTSIDE = """() => { const nav = $('channels').getBoundingClientRect();
  return [...document.querySelectorAll('#drawer-foot > *, #drawer-foot button')]
    .filter((el) => el.offsetWidth).map((el) => [el.id || el.textContent.trim(), el.getBoundingClientRect()])
    .filter(([, r]) => r.left < nav.left - 0.5 || r.right > nav.right + 0.5).map(([n]) => n); }"""


def test_the_menu_s_foot_stays_inside_the_menu_at_every_width(app):
    """Each control at the menu's foot (the install line with its buttons,
    Settings with its hint, New room) stays inside the menu: in the drawer
    of a narrow window, in the column split at the centre and at its
    narrowest."""
    from playwright.sync_api import expect
    page, stub, errors = app(viewport={"width": 700, "height": 800}, push=True, settings=True, subscriptions=SUBS)
    page.wait_for_selector(".thread-item")
    page.evaluate(FAKE_PROMPT)  # the install line, with its button
    page.click("#menu")
    expect(page.locator("#channels")).to_be_in_viewport()
    expect(page.locator("#install-btn")).to_be_visible()
    wait_for(lambda: page.evaluate("getComputedStyle($('channels')).transform") == "none")  # the drawer's slide ended
    assert page.evaluate(OUTSIDE) == []
    for width in (1000, 1100, 1280):
        page.set_viewport_size({"width": width, "height": 800})
        assert page.evaluate(OUTSIDE) == [], width
        page.locator("#nav-edge").press("Home")
        assert page.evaluate(NAV_W) == 180
        assert page.evaluate(OUTSIDE) == [], width
        page.locator("#nav-edge").press("Enter")
    assert page.evaluate(SIDEWAYS) == [0, 0]
    assert not errors, errors


def test_a_phone_gets_44px_targets_and_never_scrolls_sideways(app):
    from playwright.sync_api import expect
    import webui_stub_server as W
    page, stub, errors = app(viewport={"width": 390, "height": 844},
                             context={"has_touch": True, "is_mobile": True, "device_scale_factor": 3})
    page.wait_for_selector(".thread-item")
    # Closed, the drawer is out of the tab order.
    assert page.evaluate("getComputedStyle($('channels')).visibility") == "hidden"
    assert page.evaluate(SMALL_TARGETS) == []
    page.click("#menu")
    expect(page.locator("#channels")).to_be_in_viewport()
    assert page.evaluate(SMALL_TARGETS) == []
    page.click("#scrim", position={"x": 370, "y": 400})
    open_thread(page, "Draft a reply")
    page.evaluate("document.querySelector('.card-actions').scrollIntoView({ block: 'center' })")
    assert page.evaluate(SMALL_TARGETS) == []
    page.click("#back")
    open_thread(page, "Open a room")
    assert page.evaluate(SMALL_TARGETS) == []
    assert page.evaluate(SIDEWAYS) == [0, 0]
    stub.emit_bot(W.MAIN, "Wide: https://e.example/" + "a/" * 80 + " " + "x" * 120
                  + "\n\n```\n" + "y = 1  " * 40 + "\n```\n\n| a | b | c |\n|---|---|---|\n"
                  + "| one two three four | five six seven eight | nine ten eleven twelve |")
    page.click("#back")
    open_thread(page, "Wide:")
    assert page.evaluate(SIDEWAYS) == [0, 0]
    assert not errors, errors


# -- a room's menu: rename, archive, bring back ----------------------------

# A finger held still on an element for 700 ms, then lifted, and the tap's
# click on what is under it: what a long press sends.
PRESS = """(sel) => new Promise((done) => {
    const r = document.querySelector(sel).getBoundingClientRect();
    const [x, y] = [r.left + 20, r.top + r.height / 2];
    const at = (type) => document.elementFromPoint(x, y).dispatchEvent(new (type === 'click' ? MouseEvent : PointerEvent)(
        type, { pointerType: 'touch', bubbles: true, cancelable: true, clientX: x, clientY: y }));
    at('pointerdown');
    setTimeout(() => { at('pointerup'); at('click'); setTimeout(() => done(location.hash), 50); }, 700);
})"""


def test_a_rooms_dots_open_its_menu_and_rename_it_in_place(app):
    """With a mouse, the dots beside a room open its menu; the fixed places
    have none. Rename room puts the name in a field, Save sends it with the
    bot's "ea-" mark kept, a name in use is refused in the sheet, and the
    drawer follows the answer and the bot's channel events."""
    from playwright.sync_api import expect
    import webui_stub_server as W
    page, stub, errors = app()
    page.wait_for_selector(".thread-item")
    assert page.locator("#channel-list > a.chan .room-more").count() == 0
    expect(page.locator(".chan-row")).to_have_count(3)  # the room, the archived one and the digests
    row, menu = page.locator(f'.chan-row[data-room="{W.ROOM}"]'), page.locator("#room-menu")
    dots = row.locator(".room-more")
    expect(dots).to_have_attribute("aria-label", "Room options: example-project")
    row.hover()
    dots.click()
    expect(menu.locator("h2")).to_have_text("example-project")
    expect(menu.locator("button:not(.swatch)")).to_have_text(["Rename room", "Mute", "Archive room", "Cancel"])
    assert page.evaluate("location.hash") == "#/stream%3A9"  # the dots open no room
    menu.get_by_role("button", name="Rename room").click()
    field = menu.locator("#room-menu-name")
    expect(field).to_have_value("example-project")
    expect(field).to_be_focused()
    expect(menu.locator("button")).to_have_text(["Cancel", "Save"])
    menu.get_by_role("button", name="Cancel").click()
    expect(menu).to_be_hidden()
    row.hover()
    dots.click()
    menu.get_by_role("button", name="Rename room").click()
    field.fill("old-project")  # the archived room's name, shown without its mark
    field.press("Enter")
    expect(menu.locator(".error")).to_have_text("Not changed: Another room has that name.")
    expect(menu).to_be_visible()
    field.fill("  Atlas   paper ")
    menu.get_by_role("button", name="Save").click()
    expect(menu).to_be_hidden()
    assert stub.room_changes == [{"id": W.ROOM, "name": "ea-Atlas paper"}]
    expect(row.locator(".c-name")).to_have_text("Atlas paper")
    # a rename made elsewhere reaches the drawer as the bot's channel event
    stub.change_room(W.ROOM, name="ea-atlas")
    expect(row.locator(".c-name")).to_have_text("atlas")
    assert errors == ["Failed to load resource: the server responded with a status of 409 (Conflict)"]


def test_archive_asks_once_and_bring_back_returns_the_room(app):
    from playwright.sync_api import expect
    import webui_stub_server as W
    page, stub, errors = app()
    page.wait_for_selector(".thread-item")
    menu = page.locator("#room-menu")
    page.locator(f'.chan-row[data-room="{W.ROOM}"]').hover()
    page.locator(f'.chan-row[data-room="{W.ROOM}"] .room-more').click()
    menu.get_by_role("button", name="Archive room").click()
    expect(menu.locator("p:not(.error)")).to_have_text(
        "Archive example-project? Its conversations stay; the room moves under Archived.")
    expect(menu.locator("button")).to_have_text(["Cancel", "Archive"])
    menu.get_by_role("button", name="Archive", exact=True).click()
    expect(menu).to_be_hidden()
    assert stub.room_changes == [{"id": W.ROOM, "archived": True}]
    archived = page.locator("#channel-list details.archived")
    expect(archived.locator("summary")).to_have_text("Archived (2)")
    expect(archived.locator(".c-name")).to_have_text(["example-project", "old-project"])
    # an archived room's menu brings it back, and nothing else
    archived.locator("summary").click()
    old = archived.locator(f'.chan-row[data-room="{W.OLD}"]')
    old.hover()
    old.locator(".room-more").click()
    expect(menu.locator("button")).to_have_text(["Bring back", "Cancel"])
    menu.get_by_role("button", name="Bring back").click()
    expect(menu).to_be_hidden()
    assert stub.room_changes[-1] == {"id": W.OLD, "archived": False}
    expect(page.locator(f'#channel-list > .chan-row[data-room="{W.OLD}"] .c-name')).to_have_text("old-project")
    assert not errors, errors


def test_a_muted_channel_keeps_its_count_off_the_menu_and_the_title(app):
    """The digests start muted: their new messages show a grey count beside
    the name, add nothing to the title, and the channel keeps its place in
    the drawer. Mute in a room's menu mutes the room the same way; the
    digests' menu holds Unmute alone, and Unmute brings their count back."""
    from playwright.sync_api import expect
    import webui_stub_server as W
    page, stub, errors = app()
    page.wait_for_selector(".thread-item")
    expect(page.locator('a.chan[href="#/stream%3A9"] .badge')).to_be_visible()
    shown = lambda: int((re.match(r"\((\d+)\)", page.title()) or [0, 0])[1])  # noqa: E731
    names = lambda: page.locator("#channel-list .c-name").all_text_contents()  # noqa: E731
    before, order = shown(), names()
    stub.emit_bot(W.DIGEST, "Mail digest one")
    stub.emit_bot(W.DIGEST, "Mail digest two")
    digest = page.locator(f'.chan-row[data-room="{W.DIGEST}"]')
    expect(digest.locator(".badge.muted")).to_have_text("2")
    expect(digest.locator("a.chan")).not_to_have_class(re.compile(r"\bunread\b"))
    assert shown() == before and names() == order
    room = page.locator(f'.chan-row[data-room="{W.ROOM}"]')
    stub.emit_bot(W.ROOM, "A note in the room")
    expect(room.locator(".badge")).to_be_visible()
    before, in_room = shown(), int(room.locator(".badge").text_content())
    menu = page.locator("#room-menu")
    room.hover()
    room.locator(".room-more").click()
    menu.get_by_role("button", name="Mute").click()
    expect(menu).to_be_hidden()
    assert stub.room_changes == [{"id": W.ROOM, "muted": True}]
    expect(room.locator(".badge.muted")).to_have_text(str(in_room))
    expect(page).to_have_title(f"({before - in_room}) Tacitus" if before > in_room else "Tacitus")
    digest.hover()
    digest.locator(".room-more").click()
    expect(menu.locator("h2")).to_have_text("Mail digests")
    expect(menu.locator("button")).to_have_text(["Unmute", "Cancel"])
    menu.get_by_role("button", name="Unmute").click()
    expect(menu).to_be_hidden()
    assert stub.room_changes[-1] == {"id": W.DIGEST, "muted": False}
    expect(digest.locator(".badge:not(.muted)")).to_have_text("2")
    expect(page).to_have_title(f"({before - in_room + 2}) Tacitus")
    assert not errors, errors


def test_debug_mode_off_leaves_the_health_room_out_and_the_switch_brings_it_back(app):
    """With debug mode off the bot marks the health room hidden: the drawer
    leaves it out, and its unread report adds nothing to the title. The
    Debug mode switch in the Settings menu shows the bot's answer when the
    menu opens and saves at once; the bot's `channel` event then shows the
    room, and turning the switch off hides it again."""
    from playwright.sync_api import expect
    import webui_stub_server as W
    page, stub, errors = app(debug=False)
    page.wait_for_selector(".thread-item")
    room = page.locator(f'#channel-list a.chan[href="#/{W.HEALTH}"]')
    expect(page.locator("#channel-list a.chan .c-name").first).to_be_visible()
    expect(room).to_have_count(0)
    shown = lambda: int((re.match(r"\((\d+)\)", page.title()) or [0, 0])[1])  # noqa: E731
    before = shown()
    page.locator("#open-settings").click()
    row, box = page.locator("#debug-row"), page.locator("#debug")
    expect(row.locator(".s-label")).to_have_text("Debug mode")
    expect(row.locator(".s-detail")).to_have_text(
        "Shows the checks and reports Tacitus runs on itself. Problems you need to fix show either way.")
    expect(box).not_to_be_checked()
    row.click()
    expect(box).to_be_checked()  # at the click, before the save reaches the bot
    wait_for(lambda: stub.debug_posts == [{"on": True}])
    expect(room).to_have_count(1)
    expect(room.locator(".c-name")).to_have_text("health")
    expect(page).to_have_title(f"({before + 1}) Tacitus")
    row.click()
    expect(box).not_to_be_checked()
    wait_for(lambda: stub.debug_posts[-1] == {"on": False})
    expect(room).to_have_count(0)
    expect(page).to_have_title(f"({before}) Tacitus" if before else "Tacitus")
    # the menu opened again reads the switch from the bot
    stub.debug = True
    page.locator("#settings-close").click()
    page.locator("#open-settings").click()
    expect(box).to_be_checked()
    assert not errors, errors


def test_the_debug_switch_shows_only_where_the_bot_serves_it(app):
    from playwright.sync_api import expect
    page, stub, errors = app()
    page.wait_for_selector(".thread-item")
    page.locator("#open-settings").click()
    expect(page.locator("#tour-again")).to_be_visible()
    expect(page.locator("#debug-row")).to_be_hidden()
    assert not errors, errors


def test_a_long_press_on_a_room_opens_its_menu_as_a_sheet_on_a_phone(app):
    """On a phone the dots are gone: a long press on a room opens its menu,
    docked to the bottom with 44px buttons, and the tap that ends the press
    opens nothing, nor presses the sheet's button that opened under it. A
    long press on a fixed place opens no menu."""
    from playwright.sync_api import expect
    import webui_stub_server as W
    page, stub, errors = app(**PHONE)
    page.wait_for_selector(".thread-item")
    drawer_drawn(page)  # the scroll below holds the row, which Recent's first draw replaces
    page.click("#menu")
    wait_for(lambda: page.evaluate("document.getElementById('channels').getBoundingClientRect().left >= 0"))
    expect(page.locator(f'.chan-row[data-room="{W.ROOM}"] .room-more')).to_be_hidden()
    before = page.evaluate("location.hash")
    page.locator(f'.chan-row[data-room="{W.ROOM}"]').scroll_into_view_if_needed()  # under More, past Recent
    assert page.evaluate(PRESS, f'.chan-row[data-room="{W.ROOM}"] a.chan') == before
    menu = page.locator("#room-menu")
    expect(menu).to_be_visible()
    box = menu.bounding_box()
    assert (round(box["x"]), round(box["width"]), round(box["y"] + box["height"])) == (0, 390, 844)
    assert page.evaluate(SMALL_TARGETS) == []
    menu.get_by_role("button", name="Rename room").click()
    assert page.evaluate(SMALL_TARGETS) == []
    menu.get_by_role("button", name="Cancel").click()
    expect(menu).to_be_hidden()
    page.locator('#channel-list > a.chan[href="#/stream%3A9"]').scroll_into_view_if_needed()
    page.evaluate(PRESS, '#channel-list > a.chan[href="#/stream%3A9"]')
    expect(menu).to_be_hidden()
    assert page.evaluate(SIDEWAYS) == [0, 0]
    assert not errors, errors


ROOM_LOOK = """(sel) => { const el = document.querySelector(sel), cs = getComputedStyle(el);
  const bar = getComputedStyle(el, '::before');
  return {border: cs.borderTopColor, width: cs.borderTopWidth, fill: cs.backgroundColor,
          bar: bar.content === 'none' ? null : bar.backgroundColor}; }"""
ROOM_LINK = '#channel-list .chan-row a.chan.room[href="#/r1"]'


def test_a_room_is_a_card_in_its_color_and_its_menu_picks_another(app):
    """Each room is a card outlined in its own color, with no fill, last in
    the menu under a rule. The open room has a short bar in its color beside
    the card, and its name in the list's header sits in the same outline. A
    right-click on a room opens its menu, which offers the grid of colors
    with the one in use pressed; a pick saves it and every outline follows.
    A right-click on a place with no menu leaves the browser's own menu."""
    from playwright.sync_api import expect
    import webui_stub_server as W
    page, stub, errors = app()
    drawer_drawn(page)
    blue, orange, clear = "rgb(0, 118, 186)", "rgb(242, 114, 0)", "rgba(0, 0, 0, 0)"
    room = page.locator(ROOM_LINK)
    expect(room).to_be_visible()
    assert page.evaluate(ROOM_LOOK, ROOM_LINK) == {"border": blue, "width": "1px", "fill": clear, "bar": None}
    assert room.locator(".c-icon").count() == 0
    assert page.evaluate("$('channel-list').lastElementChild.matches('details')")  # Archived, after the rooms
    assert page.evaluate(f"document.querySelector('{ROOM_LINK}').parentElement.previousElementSibling.matches('hr')")
    room.click()
    expect(page.locator("#channel-name")).to_have_text("example-project")
    expect(page.locator("#channel-name")).to_have_class(re.compile(r"\broom-title\b"))
    look = page.evaluate(ROOM_LOOK, ROOM_LINK)
    assert (look["border"], look["fill"], look["bar"]) == (blue, clear, blue), look
    assert page.evaluate(ROOM_LOOK, "#channel-name")["border"] == blue
    # the right-click: the menu, its colors, a pick
    room.click(button="right")
    menu = page.locator("#room-menu")
    expect(menu).to_be_visible()
    swatches = menu.locator(".swatch")
    expect(swatches).to_have_count(29)
    expect(menu.locator('.swatch[aria-pressed="true"]')).to_have_count(1)
    assert swatches.nth(12).get_attribute("aria-pressed") == "true"  # #0076BA
    swatches.nth(21).click()  # #F27200
    wait_for(lambda: {"id": W.ROOM, "color": "#F27200"} in stub.room_changes)
    expect(menu).to_be_hidden()
    wait_for(lambda: page.evaluate(ROOM_LOOK, ROOM_LINK)["border"] == orange)
    assert page.evaluate(ROOM_LOOK, "#channel-name")["border"] == orange
    # no menu for Ask Tacitus: the browser keeps its own
    page.locator(f'#channel-list a.chan[href="#/{quote(W.MAIN, safe="")}"]').click(button="right")
    expect(menu).to_be_hidden()
    # leaving the room for a fixed place drops the outline from the header
    page.locator(f'#channel-list a.chan[href="#/{quote(W.MAIN, safe="")}"]').click()
    expect(page.locator("#channel-name")).not_to_have_class(re.compile(r"\broom-title\b"))
    assert not errors, errors
