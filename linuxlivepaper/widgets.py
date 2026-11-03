"""Small widgets shared by the gallery, the editor and the collage maker."""
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GdkPixbuf, Gio, GLib, Gtk  # noqa: E402

from . import library  # noqa: E402

NEW = "New Collection…"


class CollectionRow(Adw.ComboRow):
    """'Save to collection' picker: None, every collection, and New Collection… (prompts for a name)."""

    def __init__(self, title="Save to collection", select=None):
        super().__init__(title=title, subtitle="Also add it to a collection")
        self._fill(select)
        self.connect("notify::selected", self._picked)

    def _fill(self, select=None):
        self.names = [c.name for c in library.collections()]
        self._busy = True
        self.set_model(Gtk.StringList.new(["None", *self.names, NEW]))
        self.set_selected(self.names.index(select) + 1 if select in self.names else 0)
        self._busy = False

    def _picked(self, *_):
        if self._busy or self.get_selected() != len(self.names) + 1:
            return
        self.set_selected(0)
        collection_dialog(self.get_root(), lambda c: self._fill(c.name))

    def collection(self):
        i = self.get_selected()
        return library.get_collection(self.names[i - 1]) if 1 <= i <= len(self.names) else None


def folder_icon(size):
    """The desktop's own folder icon (follows the user's icon theme)."""
    return Gtk.Image(icon_name="folder", pixel_size=size)


def icon_widget(icon, size):
    if icon:
        try:
            pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(str(icon), size, size, True)
            return Gtk.Picture(paintable=Gdk.Texture.new_for_pixbuf(pb), content_fit=Gtk.ContentFit.CONTAIN,
                               width_request=size, height_request=size, can_shrink=True)
        except GLib.Error:
            pass
    return folder_icon(size)


def collection_dialog(parent, on_done, collection=None):
    """Create or edit a collection: name + optional .png icon (falls back to the system folder icon).
    on_done(collection) gets the created/updated library.Collection."""
    state = {"icon": collection.icon if collection else None, "changed": False}
    holder = Gtk.Box(halign=Gtk.Align.CENTER, height_request=112)
    entry = Gtk.Entry(text=collection.name if collection else "", activates_default=True, placeholder_text="Name")
    err = Gtk.Label(css_classes=["error", "caption"], visible=False, wrap=True)
    choose = Gtk.Button(label="Choose Icon…")
    clear = Gtk.Button(label="Use Folder Icon", css_classes=["flat"])
    buttons = Gtk.Box(spacing=6, halign=Gtk.Align.CENTER)
    buttons.append(choose)
    buttons.append(clear)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
    for w in (holder, buttons, entry, err):
        box.append(w)

    def show_icon():
        if holder.get_first_child():
            holder.remove(holder.get_first_child())
        holder.append(icon_widget(state["icon"], 112))
        clear.set_visible(bool(state["icon"]))
    show_icon()

    def picked(d, res):
        try:
            state["icon"] = d.open_finish(res).get_path()
        except GLib.Error:
            return
        state["changed"] = True
        show_icon()
    png = Gtk.FileFilter(name="PNG images")
    png.add_mime_type("image/png")
    pngs = Gio.ListStore(item_type=Gtk.FileFilter)
    pngs.append(png)
    choose.connect("clicked", lambda *_: Gtk.FileDialog(title="Choose a .png icon", filters=pngs)
                   .open(dlg, None, picked))
    clear.connect("clicked", lambda *_: (state.update(icon=None, changed=True), show_icon()))

    dlg = Adw.AlertDialog(heading="Edit Collection" if collection else "New Collection", extra_child=box,
                          default_response="ok", close_response="cancel")
    dlg.add_response("cancel", "Cancel")
    dlg.add_response("ok", "Save" if collection else "Create")
    dlg.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
    dlg.set_response_enabled("ok", bool(entry.get_text().strip()))
    dlg.set_close_response("cancel")
    entry.connect("changed", lambda e: dlg.set_response_enabled("ok", bool(e.get_text().strip())))

    def respond(d, resp):
        if resp != "ok":
            return
        try:
            if collection:
                c = library.rename_collection(collection, entry.get_text())
                if state["changed"]:
                    library.set_collection_icon(c, state["icon"])
            else:
                c = library.create_collection(entry.get_text(), state["icon"])
        except (ValueError, OSError) as ex:
            err.set_label(str(ex))
            err.set_visible(True)
            d.present(parent)  # keep it open with the message
            return
        on_done(c)
    dlg.connect("response", respond)
    dlg.present(parent)
    entry.grab_focus()
    return dlg
