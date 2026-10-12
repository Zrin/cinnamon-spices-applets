"""
KNOWN ISSUES

- This version is not as fast to load as the GJS-only version.
  I have no quantitative data, but since the code is essentially the same,
  I'd say that the extra delay is due exclusively to starting a new Python process.
  Especially because submenus load as fast as always.

- This script is triggered on mouse release. If you double-click on the applet, the mouse will have been released twice.
  The second trigger will still work as expected, but the first release event will trigger a process that never terminates,
  because it seems that popup_at_rect never shows the menu if the mouse is held at the time it's called.
  So, just don't double-click on the applet. None of the built-in ones requires double-clicking, after all.

- If this applet is on a panel set to automatically hide, the panel will hide as soon as any submenu opens.
  This is otherwise completely harmless.
  I don't think there is an easy solution here; attempting to "grab" the same way Cinnamon's PopupMenuManager does
  just ends up freezing the whole desktop environment.
  And even the XApp Status Icons have the same issue (I tried the client for Zoom, the video conference app, which puts a status icon there).
"""

#!/usr/bin/python3

import subprocess
import sys
import os

import json
from collections import defaultdict

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GLib", "2.0")
gi.require_version("XApp", "1.0")
from gi.repository import Gio, Gtk, Gdk, GLib, XApp

import gettext
UUID = "directory-menu@torchipeppo"
HOME = os.path.expanduser("~")
gettext.bindtextdomain(UUID, os.path.join(HOME, ".local/share/locale"))
gettext.textdomain(UUID)

# low number is higher priority
STANDARD_PRIORITY = 50
FAVORITE_PRIORITY_MOD = -30
PINNED_PRIORITY_MOD = -10
DIRECTORY_PRIORITY_MOD = -1

def _(message: str) -> str:
    return gettext.gettext(message)

def log(message):
    with open("/tmp/DM-remake-log.txt", "a") as f:
        f.write(str(message) + "\n")
    print(message, file=sys.stderr)

def warn(message):
    # stderr of this script ends up in ~/.xsession-errors
    print(f"{UUID}: {message}", file=sys.stderr)

ENUMERATE_ATTRIBUTES = 'standard::*,metadata::pinned-to-top,time::modified,time::modified-usec'

TERMINAL_SCHEMA = "org.cinnamon.desktop.default-applications.terminal"
# tried in this order if the configured terminal is unset or fails to start
FALLBACK_TERMINALS = ["x-terminal-emulator", "gnome-terminal", "xterm"]


def modification_sort_key(info):
    try:
        return -info.get_modification_date_time().to_unix()
    except AttributeError:  # in case info.get_modification_date_time() is None
        log(f"Unknown last edit time: {info.display_name}")
        return 0

# MEMO keep these in line with settings-scheme.json !!
SORT_KEYS = [
    # sort by name
    lambda info: info.display_name.lower(),
    # sort by date modified
    modification_sort_key,
]


class Cassettone:

    # https://lazka.github.io/pgi-docs/index.html#Gtk-3.0/classes/ImageMenuItem.html
    def new_image_menu_item(self, label_text, icon):
        box = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 6)
        label = Gtk.Label.new(label_text)
        menu_item = Gtk.MenuItem.new()

        box.add(icon)
        box.add(label)

        menu_item.add(box)

        return menu_item

    def new_placeholder_item(self, label_text):
        item = Gtk.MenuItem.new_with_label(label_text)
        item.set_sensitive(False)
        return item

    def list_directory(self, directory):
        """Return the Gio.FileInfo of every (visible) child. Raises GLib.Error if unreadable."""
        dir_iter = directory.enumerate_children(
            ENUMERATE_ATTRIBUTES,
            Gio.FileQueryInfoFlags.NONE,
            None
        )
        infos = []
        try:
            info = dir_iter.next_file(None)
            while info is not None:
                if (not info.get_is_hidden() or self.show_hidden):     # // <-- skip hidden files
                    infos.append(info)
                info = dir_iter.next_file(None)
        finally:
            dir_iter.close(None)
        return infos

    def populate_menu_with_directory(self, menu, directory_uri):
        directory = Gio.File.new_for_uri(directory_uri)

        # Read the directory before adding anything to the menu.
        # If it doesn't exist or can't be read, show a placeholder instead of
        # letting the GLib.Error escape: for the base directory that would kill
        # the script before the menu is ever shown.
        try:
            infos = self.list_directory(directory)
        except GLib.Error as e:
            warn(f"Cannot read {directory_uri}: {e.message}")
            menu.append(self.new_placeholder_item(_("Empty or Access Denied")))
            return

        # // First, the two directory actions: Open Folder and Open In Terminal

        if self.show_header:
            open_image = Gtk.Image.new_from_icon_name("folder", Gtk.IconSize.MENU)
            open_item = self.new_image_menu_item(_("Open Folder"), open_image)
            open_item.connect("activate", lambda _: self.launch(directory.get_uri(), Gtk.get_current_event_time()))
            menu.append(open_item)

            term_image = Gtk.Image.new_from_icon_name("terminal", Gtk.IconSize.MENU)
            term_item = self.new_image_menu_item(_("Open in Terminal"), term_image)
            term_item.connect("activate", lambda _: self.open_terminal_at_path(directory.get_path()))
            menu.append(term_item)

            menu.append(Gtk.SeparatorMenuItem.new())

        # log(directory_uri)

        if not infos:
            menu.append(self.new_placeholder_item(_("Empty")))
            return

        priority_bins = defaultdict(list)

        for info in infos:
            info.display_name = info.get_display_name()
            info.content_type = info.get_content_type()
            # get_child() takes the on-disk name, so it also works for names that aren't valid UTF-8
            # (get_child_for_display_name() raises on those)
            info.file = directory.get_child(info.get_name())
            info.is_directory = (info.get_content_type() == "inode/directory")

            priority = STANDARD_PRIORITY
            if self.favorites_first and self.favorites.find_by_uri(info.file.get_uri()):
                priority += FAVORITE_PRIORITY_MOD
            if self.pinned_first and info.get_attribute_as_string("metadata::pinned-to-top") == "true":
                priority += PINNED_PRIORITY_MOD

            if info.is_directory:
                priority += DIRECTORY_PRIORITY_MOD

            priority_bins[priority].append(info)

        for priority in sorted(priority_bins.keys()):
            lst = priority_bins[priority]
            lst.sort(key = SORT_KEYS[self.order_by])
            for info in lst:
                self.add_to_menu_from_gioinfo(menu, info)

    def add_to_menu_from_gioinfo(self, menu, info):
        display_text = info.display_name
        if self.character_limit > -1 and len(display_text) > self.character_limit:
            display_text = display_text[:self.character_limit] + "..."

        icon = Gio.content_type_get_icon(info.content_type)
        image = Gtk.Image.new_from_gicon(icon, Gtk.IconSize.MENU)

        uri = info.file.get_uri()

        item = self.new_image_menu_item(display_text, image)

        if info.is_directory:
            subMenu = self.create_subdirectory_submenu(uri)
            item.set_submenu(subMenu)
        else:
            item.connect("activate", lambda _: self.launch(uri, Gtk.get_current_event_time()))

        menu.append(item)

    def create_subdirectory_submenu(self, uri):
        subMenu = Gtk.Menu.new()
        subMenu.set_reserve_toggle_size(False)

        def f(_):
            self.populate_menu_with_directory(subMenu, uri)
            subMenu.show_all()

        subMenu.connect("show", f)

        subMenu.connect("hide", lambda _: self.destroy_all_children_later(subMenu))

        return subMenu

    def launch(self, uri, timestamp):
        print(json.dumps({
            "action": "launch_default_for_uri",
            "uri": uri,
            "timestamp": timestamp,
        }))
        subprocess.run(['/usr/bin/xdg-open', uri], check=False)

    def open_terminal_at_path(self, path):
        print(json.dumps({
            "action": "open_terminal_at_path",
            "path": path,
        }))
        spawn_flags = GLib.SpawnFlags.SEARCH_PATH | GLib.SpawnFlags.STDOUT_TO_DEV_NULL | GLib.SpawnFlags.STDERR_TO_DEV_NULL
        for argv in self.get_terminal_commands():
            if os.path.basename(argv[0]) == "gnome-terminal":
                # gnome-terminal opens its windows from a separate server process,
                # so tell it the directory explicitly instead of relying on our cwd
                argv = argv[:1] + ["--working-directory=" + path] + argv[1:]
            try:
                GLib.spawn_async(working_directory=path, argv=argv, flags=spawn_flags)
                return
            except GLib.Error as e:
                # e.g. program not installed, or path not accessible
                warn(f"Could not start terminal {argv[0]!r} in {path!r}: {e.message}")
        warn("Could not start any terminal")

    def get_terminal_commands(self):
        """Candidate argvs: the terminal configured in Cinnamon first, then the fallbacks."""
        commands = []

        # Gio.Settings.new() aborts the whole process if the schema is missing, so check first
        schema_source = Gio.SettingsSchemaSource.get_default()
        if schema_source is not None and schema_source.lookup(TERMINAL_SCHEMA, True) is not None:
            exec_line = Gio.Settings.new(TERMINAL_SCHEMA).get_string("exec").strip()
            if exec_line:
                try:
                    # handles quoting, unlike str.split()
                    _parsed, argv = GLib.shell_parse_argv(exec_line)
                    commands.append(argv)
                except GLib.Error as e:
                    warn(f"Cannot parse terminal command {exec_line!r}: {e.message}")

        for name in FALLBACK_TERMINALS:
            if not any(os.path.basename(argv[0]) == name for argv in commands):
                commands.append([name])
        return commands

    def destroy_subitem_later(self, subItem):
        def g2():
            subItem.destroy()
            return False
        # // destroy at some future instant, but not right now so we have time for the activate event
        GLib.idle_add(priority=GLib.PRIORITY_HIGH_IDLE, function=g2)

    def destroy_all_children_later(self, menu):
        menu.foreach(self.destroy_subitem_later)


    # https://github.com/linuxmint/xapp/blob/master/libxapp/xapp-status-icon.c#L197
    def synthesize_event(self):
        display = Gdk.Display.get_default()
        seat = display.get_default_seat()
        pointer = seat.get_pointer()

        if self.orientation == int(Gtk.PositionType.TOP):
            posx = self.x
            posy = self.y - self.PRIV_ICON_SIZE
            rect_anchor = Gdk.Gravity.SOUTH_WEST
            menu_anchor = Gdk.Gravity.NORTH_WEST
        elif self.orientation == int(Gtk.PositionType.LEFT):
            posx = self.x - self.PRIV_ICON_SIZE
            posy = self.y
            rect_anchor = Gdk.Gravity.NORTH_EAST
            menu_anchor = Gdk.Gravity.NORTH_WEST
        elif self.orientation == int(Gtk.PositionType.RIGHT):
            posx = self.x
            posy = self.y
            rect_anchor = Gdk.Gravity.NORTH_WEST
            menu_anchor = Gdk.Gravity.NORTH_EAST
        else: # int(Gtk.PositionType.BOTTOM) is default
            posx = self.x
            posy = self.y
            rect_anchor = Gdk.Gravity.NORTH_WEST
            menu_anchor = Gdk.Gravity.SOUTH_WEST

        attributes = Gdk.WindowAttr()
        attributes.window_type = Gdk.WindowType.CHILD
        attributes.x = posx
        attributes.y = posy
        attributes.width = self.PRIV_ICON_SIZE
        attributes.height = self.PRIV_ICON_SIZE

        attributes_mask = Gdk.WindowAttributesType.X | Gdk.WindowAttributesType.Y

        window = Gdk.Window.new(None, attributes, attributes_mask)

        win_rect = Gdk.Rectangle()
        win_rect.x = 0
        win_rect.y = 0
        win_rect.width = self.PRIV_ICON_SIZE
        win_rect.height = self.PRIV_ICON_SIZE

        event = Gdk.Event.new(Gdk.EventType.BUTTON_RELEASE)
        event.any.window = window
        event.button.device = pointer

        return event, window, win_rect, rect_anchor, menu_anchor


    # this is the main entry point
    def build_and_show(self, args):

        self.main_menu = Gtk.Menu.new()
        self.main_menu.set_reserve_toggle_size(False)

        def on_main_menu_hidden(_):
            self.destroy_all_children_later(self.main_menu)
            Gtk.main_quit()

        self.main_menu.connect("hide", on_main_menu_hidden)

        self.starting_uri = args["starting_uri"]
        self.show_hidden = args["show_hidden"]
        self.x = args["x"]
        self.y = args["y"]
        self.orientation = args["orientation"]
        self.character_limit = args["character_limit"]
        self.favorites_first = args["favorites_first"]
        self.pinned_first = args["pinned_first"]
        self.order_by = args["order_by"]
        self.show_header = args["show_header"]

        self.favorites = XApp.Favorites.get_default()

        # https://github.com/linuxmint/xapp/blob/master/libxapp/xapp-status-icon.c#L222
        # would be nice to determine it somehow, but it might not be so important right now
        self.PRIV_ICON_SIZE = 16

        self.populate_menu_with_directory(self.main_menu, self.starting_uri)

        self.main_menu.show_all()

        if not Gtk.Widget.get_realized(self.main_menu):
            self.main_menu.realize()


        event, window, win_rect, rect_anchor, menu_anchor = self.synthesize_event()


        self.main_menu.rect_window = window   # invece di set_data, facciamo direttamente così nel binding Python
        self.main_menu.window = window
        self.main_menu.anchor_hints = Gdk.AnchorHints.SLIDE_X | Gdk.AnchorHints.SLIDE_Y | Gdk.AnchorHints.RESIZE_X | Gdk.AnchorHints.RESIZE_Y

        self.main_menu.popup_at_rect(
            window,
            win_rect,
            rect_anchor,
            menu_anchor,
            event
        )

        # Make sure we quit the main loop if the menu is closed, there are cases where the menu is not mapped
        # and then it remains running in the background like a zombie process. This is happening for example
        # some times when the shortcut to open the menu is triggered, for some reason the menu is not mapped
        # and if the shortcut is triggered again, another process is started, leading to multiple zombie processes.
        GLib.timeout_add(200, lambda: Gtk.main_quit() if not self.main_menu.get_mapped() else False)

        Gtk.main()
        # this is a blocking call

        window.destroy()

        # This shouldn't be necessary in python.
        # event.free()


# main
args = json.loads(sys.argv[1])
Cassettone().build_and_show(args)
