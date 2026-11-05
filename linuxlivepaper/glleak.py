"""Work around GTK's media sink leaking one GstGLContext per stream (GTK ≤ 4.22, fixed upstream by
"gtkgstsink: release GL context on disposal", 9139597a74). Every leaked context keeps a GL thread, a display
thread and ~9 NVIDIA fds open, so after ~100 played clips the process hits the fd limit and PulseAudio aborts
("cannot create wakeup pipe"). We drop GTK's forgotten reference when the sink dies.

Pure ctypes on purpose: a PyGObject emission hook on GstBin crashes, because it wraps half-constructed elements."""
import ctypes as C
import os

P = C.c_void_p
_keep = []  # ctypes callbacks must outlive the process


def _bind():
    go = C.CDLL("libgobject-2.0.so.0")
    gst = C.CDLL("libgstreamer-1.0.so.0")
    gl = C.CDLL("libgstgl-1.0.so.0")
    for lib, name, res, args in [
        (go, "g_value_get_object", P, [P]), (go, "g_type_name_from_instance", C.c_char_p, [P]),
        (go, "g_object_unref", None, [P]), (go, "g_object_weak_ref", None, [P, P, P]),
        (go, "g_signal_lookup", C.c_uint, [C.c_char_p, C.c_size_t]),
        (go, "g_signal_add_emission_hook", C.c_ulong, [C.c_uint, C.c_uint, P, P, P]),
        (gst, "gst_init", None, [P, P]), (gst, "gst_bin_get_type", C.c_size_t, []),
        (gst, "gst_element_get_static_pad", P, [P, C.c_char_p]), (gst, "gst_object_unref", None, [P]),
        (gst, "gst_query_new_context", P, [C.c_char_p]), (gst, "gst_pad_query", C.c_int, [P, P]),
        (gst, "gst_query_parse_context", None, [P, C.POINTER(P)]), (gst, "gst_mini_object_unref", None, [P]),
        (gst, "gst_context_get_structure", P, [P]),
        (gst, "gst_structure_get", C.c_int, [P, C.c_char_p, C.c_size_t, C.POINTER(P), P]),
        (gl, "gst_gl_context_get_type", C.c_size_t, [])]:
        f = getattr(lib, name)
        f.restype, f.argtypes = res, args
    return go, gst, gl


def install():
    if os.environ.get("LLP_NOGLFIX"):  # A/B testing the leak
        return
    from gi.repository import Gtk
    if (Gtk.get_major_version(), Gtk.get_minor_version()) > (4, 22):
        return
    try:
        go, gst, gl = _bind()
    except (OSError, AttributeError):
        return
    gst.gst_init(None, None)
    gl_type = gl.gst_gl_context_get_type()

    @C.CFUNCTYPE(None, P, P)
    def gone(ctx, _sink):
        # We hold one ref. Exactly two left → GTK's leaked ref is still there, drop it. One left → this GTK already
        # releases it (backported fix), so only drop ours. ref_count is the guint right after the GTypeInstance.
        if C.c_uint.from_address(ctx + C.sizeof(P)).value >= 2:
            go.g_object_unref(ctx)
        go.g_object_unref(ctx)

    @C.CFUNCTYPE(C.c_int, P, C.c_uint, P, P)
    def added(_hint, _n, params, _data):
        el = go.g_value_get_object(params + 24)  # params[1]: the added element (GValue is 24 bytes)
        if el and go.g_type_name_from_instance(el) == b"GtkGstSink":
            pad = gst.gst_element_get_static_pad(el, b"sink")
            if pad:
                q = gst.gst_query_new_context(b"gst.gl.local_context")
                ctx, c = P(), P()
                if gst.gst_pad_query(pad, q):
                    gst.gst_query_parse_context(q, C.byref(c))
                    if c:  # gst_structure_get hands us a ref; keep it until the sink is gone
                        gst.gst_structure_get(gst.gst_context_get_structure(c), b"context", gl_type, C.byref(ctx), None)
                if ctx:
                    go.g_object_weak_ref(el, C.cast(gone, P), ctx)
                gst.gst_mini_object_unref(q)
                gst.gst_object_unref(pad)
        return 1

    _keep.extend((gone, added))
    sig = go.g_signal_lookup(b"element-added", gst.gst_bin_get_type())
    go.g_signal_add_emission_hook(sig, 0, C.cast(added, P), None, None)
