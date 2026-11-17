// Writes the focused window's pid, class and title to $XDG_RUNTIME_DIR/claude-board-focus.json
// whenever the focus or that window's title changes. Read by the claude-board agent.
import GLib from 'gi://GLib';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

export default class ClaudeBoardFocus extends Extension {
    enable() {
        this._path = GLib.build_filenamev([GLib.get_user_runtime_dir(), 'claude-board-focus.json']);
        this._win = null;
        this._titleId = 0;
        this._focusId = global.display.connect('notify::focus-window', () => this._track());
        this._track();
    }

    disable() {
        global.display.disconnect(this._focusId);
        this._untrack();
        try { GLib.unlink(this._path); } catch (e) {}
    }

    _untrack() {
        if (this._win && this._titleId) {
            try { this._win.disconnect(this._titleId); } catch (e) {}
        }
        this._win = null;
        this._titleId = 0;
    }

    _track() {
        this._untrack();
        this._win = global.display.focus_window;
        if (this._win)
            this._titleId = this._win.connect('notify::title', () => this._write());
        this._write();
    }

    _write() {
        const w = this._win;
        const data = {
            pid: w ? w.get_pid() : 0,
            wm_class: (w && w.get_wm_class()) || '',
            title: (w && w.get_title()) || '',
            time: Date.now() / 1000,
        };
        try {
            GLib.file_set_contents(this._path, JSON.stringify(data));
        } catch (e) {
            logError(e, 'claude-board-focus');
        }
    }
}
