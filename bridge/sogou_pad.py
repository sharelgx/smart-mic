"""One dictation session in a project-owned AppKit field, controlled over inherited pipes."""

import json
import os
import sys
import time
from pathlib import Path

import AppKit as A
import objc


def emit(**message):
    print(json.dumps(message, ensure_ascii=False), flush=True)


def edit_menu(app):
    # AppKit routes Cmd-V (also used by dictation software) through the menu.
    menu = A.NSMenu.alloc().init()
    edit = A.NSMenu.alloc().initWithTitle_("编辑")
    edit.addItemWithTitle_action_keyEquivalent_("粘贴", "paste:", "v")
    item = A.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("编辑", None, "")
    menu.addItem_(item)
    item.setSubmenu_(edit)
    app.setMainMenu_(menu)


def capture():
    app = A.NSApplication.sharedApplication()
    app.setActivationPolicy_(A.NSApplicationActivationPolicyAccessory)
    edit_menu(app)
    app.finishLaunching()
    setup = json.loads(sys.stdin.readline())
    target = setup['target']
    bounds = A.NSMakeRect(0, 0, 460, 100)
    style = A.NSWindowStyleMaskTitled | A.NSWindowStyleMaskClosable
    win = A.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        bounds, style, A.NSBackingStoreBuffered, False)
    win.setReleasedWhenClosed_(False)
    win.setTitle_(f"搜狗听写 · {Path(target['cwd']).name} · {target['title'][:35]}")
    scroll = A.NSScrollView.alloc().initWithFrame_(bounds)
    scroll.setHasVerticalScroller_(True)
    field = A.NSTextView.alloc().initWithFrame_(bounds)
    field.setRichText_(False)
    field.setAllowsUndo_(False)
    field.setAutomaticSpellingCorrectionEnabled_(False)
    field.setFont_(A.NSFont.systemFontOfSize_(19))
    field.setTextContainerInset_((10, 8))
    scroll.setDocumentView_(field)
    win.setContentView_(scroll)
    win.center()
    win.makeKeyAndOrderFront_(None)
    win.makeFirstResponder_(field)
    app.activateIgnoringOtherApps_(True)

    os.set_blocking(sys.stdin.fileno(), False)
    pending = b""
    start = last_change = time.monotonic()
    ready = False
    finishing = None
    previous = ""
    while True:
        with objc.autorelease_pool():
            event = app.nextEventMatchingMask_untilDate_inMode_dequeue_(
                A.NSEventMaskAny, A.NSDate.dateWithTimeIntervalSinceNow_(0.035),
                A.NSDefaultRunLoopMode, True)
            if event is not None:
                app.sendEvent_(event)
            app.updateWindows()

            now = time.monotonic()
            if not win.isVisible():
                raise ValueError("听写窗口已关闭")
            focused = win.isKeyWindow() and win.firstResponder() == field and app.isActive()
            if not ready and focused:
                ready = True
                emit(event="ready")
            elif ready and not focused:
                raise ValueError("听写框失去焦点，本次不发送")
            elif not ready and now - start > 4:
                raise ValueError("未能聚焦本次听写框")

            try:
                data = os.read(sys.stdin.fileno(), 1024)
            except BlockingIOError:
                data = None
            if data == b"":
                return
            if data:
                pending += data
                if len(pending) > 2048:
                    raise ValueError("听写控制消息过长")
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    message = json.loads(line)
                    if message == {"cmd": "cancel"}:
                        return
                    if message != {"cmd": "finish"} or not ready or finishing is not None:
                        raise ValueError("听写控制消息顺序错误")
                    finishing = now
                    win.setTitle_("正在等待搜狗完成听写")

            current = str(field.string())
            if current != previous or field.hasMarkedText():
                previous, last_change = current, now
            if len(current) > 16000:
                raise ValueError("听写内容超过本段长度限制")
            if finishing is not None:
                # Sogou commits through native input/paste. Delay until the field
                # settles after the explicit stop, including any final IME edits.
                if current.strip() and not field.hasMarkedText() and now - max(
                        last_change, finishing) >= 1.6:
                    emit(event="final", text=current.strip())
                    return
                if now - finishing > 8:
                    raise ValueError("搜狗没有提交稳定的听写文字")
            if now - start > 82:
                raise ValueError("本次听写超时")


if __name__ == "__main__":
    try:
        capture()
    except Exception as exc:
        # Only our fixed diagnostic messages may be sent to the parent.
        reason = str(exc) if type(exc) is ValueError else "无法使用本次听写框"
        emit(event="error", reason=reason)
        sys.exit(1)
