// --- armorpaint-mcp native extension ----------------------------------------------------------
//
// ONE plugin binding, mcp_ext_call(op, args, prefix), that gives the armorpaint-mcp bridge the
// operations ArmorPaint's minic API has no binding for: layer management, undo/redo, export
// format / bit depth / preset, bake parameters and bake runs, render settings (tone and LUT),
// texture-set resolution, live project lists, camera views and a file-based viewport capture.
//
// This file is NOT part of ArmorPaint. patch/apply_ext_patch.py copies it into a checkout as
// paint/sources/mcp_ext.c and #includes it at the end of main.c's unity build, so every
// ArmorPaint function and global -- including the file-static ones in tab_layers.c and
// bake_texture_node.c -- is in scope. Every entry point mirrors what ArmorPaint's own UI does
// for the same action, including the history (undo) step it pushes.
//
// Contract (see docs/UPSTREAM_CHANGES.md):
//   * op      -- the bridge op name, e.g. "layer_list"
//   * args    -- the request map from json_parse_to_map(); every value is a char*
//   * prefix  -- key prefix for batched requests ("" for a plain request); arguments are read
//                as <prefix>a_<name>
//   * returns -- "1<json object>" on success, "0{"code":..,"message":..}" on failure. The
//                string is owned here and overwritten by the next call; the bridge copies it.
//
// Nothing here is reached unless a request asks for it, and no existing ArmorPaint behaviour
// changes. Marker: armorpaint-mcp.

#define MCP_EXT_VERSION 1

// ---- output buffer ------------------------------------------------------------------------------

static char *mcp_buf     = NULL;
static int   mcp_buf_len = 0;
static int   mcp_buf_cap = 0;
static int   mcp_first   = 1; // no comma before the next key

static void mcp_put(const char *s) {
	int n = (int)strlen(s);
	if (mcp_buf_len + n + 1 > mcp_buf_cap) {
		int cap = mcp_buf_cap < 1024 ? 1024 : mcp_buf_cap;
		while (mcp_buf_len + n + 1 > cap) {
			cap *= 2;
		}
		char *grown = realloc(mcp_buf, cap);
		if (grown == NULL) {
			return;
		}
		mcp_buf     = grown;
		mcp_buf_cap = cap;
	}
	memcpy(mcp_buf + mcp_buf_len, s, n);
	mcp_buf_len += n;
	mcp_buf[mcp_buf_len] = '\0';
}

static void mcp_put_str(const char *s) {
	mcp_put("\"");
	if (s != NULL) {
		char one[8];
		for (const unsigned char *p = (const unsigned char *)s; *p; ++p) {
			if (*p == '"' || *p == '\\') {
				one[0] = '\\';
				one[1] = (char)*p;
				one[2] = '\0';
			}
			else if (*p < 0x20) {
				snprintf(one, sizeof(one), "\\u%04x", *p);
			}
			else {
				one[0] = (char)*p;
				one[1] = '\0';
			}
			mcp_put(one);
		}
	}
	mcp_put("\"");
}

static void mcp_key(const char *k) {
	if (!mcp_first) {
		mcp_put(",");
	}
	mcp_first = 0;
	mcp_put_str(k);
	mcp_put(":");
}

static void mcp_kv_s(const char *k, const char *v) {
	mcp_key(k);
	if (v == NULL) {
		mcp_put("null");
	}
	else {
		mcp_put_str(v);
	}
}

static void mcp_kv_i(const char *k, int v) {
	char tmp[32];
	snprintf(tmp, sizeof(tmp), "%d", v);
	mcp_key(k);
	mcp_put(tmp);
}

static void mcp_kv_f(const char *k, double v) {
	char tmp[64];
	if (v != v || v > 1e30 || v < -1e30) {
		snprintf(tmp, sizeof(tmp), "null");
	}
	else {
		snprintf(tmp, sizeof(tmp), "%.6g", v);
	}
	mcp_key(k);
	mcp_put(tmp);
}

static void mcp_kv_b(const char *k, bool v) {
	mcp_key(k);
	mcp_put(v ? "true" : "false");
}

// Nested object/array under a key (or bare, inside an array, when k is NULL).
static void mcp_open(const char *k, const char *bracket) {
	if (k != NULL) {
		mcp_key(k);
	}
	else if (!mcp_first) {
		mcp_put(",");
	}
	mcp_put(bracket);
	mcp_first = 1;
}

static void mcp_close(const char *bracket) {
	mcp_put(bracket);
	mcp_first = 0;
}

static void mcp_begin_ok(void) {
	mcp_buf_len = 0;
	mcp_put("1{");
	mcp_first = 1;
}

static char *mcp_end_ok(void) {
	mcp_put("}");
	return mcp_buf;
}

static char *mcp_fail(const char *code, const char *fmt, ...) {
	char    msg[512];
	va_list ap;
	va_start(ap, fmt);
	vsnprintf(msg, sizeof(msg), fmt, ap);
	va_end(ap);
	mcp_buf_len = 0;
	mcp_put("0{");
	mcp_first = 1;
	mcp_kv_s("code", code);
	mcp_kv_s("message", msg);
	mcp_put("}");
	return mcp_buf;
}

// ---- arguments ----------------------------------------------------------------------------------

static any_map_t  *mcp_args   = NULL;
static const char *mcp_prefix = "";

static char *mcp_arg(const char *name) {
	if (mcp_args == NULL) {
		return NULL;
	}
	char key[128];
	snprintf(key, sizeof(key), "%sa_%s", mcp_prefix, name);
	char *v = any_map_get(mcp_args, key);
	if (v == NULL && mcp_prefix[0] == '\0') {
		v = any_map_get(mcp_args, (char *)name);
	}
	return v;
}

static bool mcp_has(const char *name) {
	char *v = mcp_arg(name);
	return v != NULL && v[0] != '\0';
}

static int mcp_arg_i(const char *name, int dflt) {
	char *v = mcp_arg(name);
	if (v == NULL || v[0] == '\0') {
		return dflt;
	}
	if (strcmp(v, "true") == 0) {
		return 1;
	}
	if (strcmp(v, "false") == 0) {
		return 0;
	}
	return (int)strtol(v, NULL, 10);
}

static double mcp_arg_f(const char *name, double dflt) {
	char *v = mcp_arg(name);
	if (v == NULL || v[0] == '\0') {
		return dflt;
	}
	return strtod(v, NULL);
}

static bool mcp_arg_b(const char *name, bool dflt) {
	char *v = mcp_arg(name);
	if (v == NULL || v[0] == '\0') {
		return dflt;
	}
	return strcmp(v, "true") == 0 || strcmp(v, "1") == 0;
}

// ---- helpers --------------------------------------------------------------------------------------

static bool mcp_have_project(void) {
	return g_project != NULL && g_project->_ != NULL && g_project->_->layers != NULL;
}

static const char *mcp_blend_names[] = {"mix",         "darken",       "multiply",   "burn",     "lighten", "screen",
                                        "dodge",       "add",          "overlay",    "soft_light", "linear_light", "difference",
                                        "subtract",    "divide",       "hue",        "saturation", "color",    "value"};
#define MCP_BLEND_COUNT 18

static const char *mcp_layer_kind(slot_layer_t *l) {
	if (slot_layer_is_group(l)) {
		return "group";
	}
	if (slot_layer_is_filter(l)) {
		return "filter";
	}
	if (slot_layer_is_mask(l)) {
		return "mask";
	}
	return "layer";
}

// Resolve the target layer: "index" (position in the stack, 0 = bottom, as ArmorPaint stores it),
// else "name" (first match), else "layer_id", else the selected layer.
static slot_layer_t *mcp_target_layer(char **why) {
	slot_layer_t_array_t *ls = g_project->_->layers;
	if (mcp_has("index")) {
		int i = mcp_arg_i("index", -1);
		if (i < 0 || i >= ls->length) {
			*why = "index out of range";
			return NULL;
		}
		return ls->buffer[i];
	}
	if (mcp_has("name")) {
		char *name = mcp_arg("name");
		for (int i = 0; i < ls->length; ++i) {
			if (ls->buffer[i]->name != NULL && strcmp(ls->buffer[i]->name, name) == 0) {
				return ls->buffer[i];
			}
		}
		*why = "no layer with that name";
		return NULL;
	}
	if (mcp_has("layer_id")) {
		int id = mcp_arg_i("layer_id", -1);
		for (int i = 0; i < ls->length; ++i) {
			if (ls->buffer[i]->id == id) {
				return ls->buffer[i];
			}
		}
		*why = "no layer with that id";
		return NULL;
	}
	if (g_context->layer == NULL) {
		*why = "no layer is selected";
	}
	return g_context->layer;
}

static void mcp_emit_layer(slot_layer_t *l, int index) {
	slot_layer_t_array_t *ls = g_project->_->layers;
	mcp_open(NULL, "{");
	mcp_kv_i("index", index);
	mcp_kv_i("id", l->id);
	mcp_kv_s("name", l->name);
	mcp_kv_s("kind", mcp_layer_kind(l));
	mcp_kv_b("selected", l == g_context->layer);
	mcp_kv_b("visible", l->visible);
	mcp_kv_f("opacity", l->mask_opacity);
	int b = (int)l->blending;
	mcp_kv_s("blending", (b >= 0 && b < MCP_BLEND_COUNT) ? mcp_blend_names[b] : "unknown");
	mcp_kv_i("parent_index", l->parent == NULL ? -1 : array_index_of(ls, l->parent));
	mcp_kv_b("is_fill", l->fill_material != NULL);
	if (l->fill_material != NULL && l->fill_material->canvas != NULL) {
		mcp_kv_s("fill_material", l->fill_material->canvas->name);
	}
	mcp_kv_i("object_mask", l->object_mask);
	mcp_kv_f("scale", l->scale);
	mcp_kv_f("angle", l->angle);
	mcp_kv_i("uv_type", (int)l->uv_type);
	if (slot_layer_is_layer(l)) {
		mcp_kv_b("has_masks", slot_layer_has_masks(l, false));
	}
	mcp_close("}");
}

static void mcp_remap_pointers(i32_map_t *pointers) {
	for (int i = 0; i < g_project->_->materials->length; ++i) {
		slot_material_t *m = g_project->_->materials->buffer[i];
		tab_layers_remap_layer_pointers(m->canvas->nodes, tab_layers_fill_layer_map(pointers));
	}
}

static void mcp_refresh_layers(void) {
	make_material_parse_mesh_material();
	g_context->layers_preview_dirty = true;
	g_context->ddirty               = 2;
	ui_base_hwnds->buffer[TAB_AREA_SIDEBAR0]->redraws = 2;
}

static char *mcp_layer_ok(slot_layer_t *l) {
	mcp_begin_ok();
	if (l != NULL && mcp_have_project()) {
		mcp_key("layer");
		mcp_first = 1; // the object below is the key's value, not a list element
		mcp_emit_layer(l, array_index_of(g_project->_->layers, l));
	}
	mcp_kv_i("layer_count", g_project->_->layers->length);
	return mcp_end_ok();
}

// ---- viewport capture -----------------------------------------------------------------------------

static gpu_texture_t *mcp_capture_tex = NULL;
static int            mcp_capture_w   = 0;
static int            mcp_capture_h   = 0;

static char *mcp_op_capture_viewport(void) {
	char *path = mcp_arg("path");
	if (path == NULL || path[0] == '\0') {
		return mcp_fail("bad_args", "missing 'path'");
	}
	int w = mcp_arg_i("width", 1024);
	int h = mcp_arg_i("height", 1024);
	if (w < 16 || w > 4096 || h < 16 || h > 4096) {
		return mcp_fail("bad_args", "width and height must be 16..4096");
	}
	if (mcp_capture_tex == NULL || mcp_capture_w != w || mcp_capture_h != h) {
		if (mcp_capture_tex != NULL) {
			gpu_delete_texture(mcp_capture_tex);
		}
		mcp_capture_tex = gpu_create_render_target(w, h, GPU_TEXTURE_FORMAT_RGBA32);
		mcp_capture_w   = w;
		mcp_capture_h   = h;
	}
	if (mcp_capture_tex == NULL) {
		return mcp_fail("internal", "gpu_create_render_target returned null");
	}
	viewport_capture_screenshot_to(mcp_capture_tex, 0.0f, 0.0f, (float)w, (float)h);
	iron_write_png(path, gpu_get_texture_pixels(mcp_capture_tex), w, h, 0);
	g_context->ddirty = 2;
	mcp_begin_ok();
	mcp_kv_s("path", path);
	mcp_kv_i("width", w);
	mcp_kv_i("height", h);
	mcp_kv_b("exists", iron_file_exists(path));
	mcp_kv_s("method", "mcp_ext (iron_write_png)");
	return mcp_end_ok();
}

// ---- layers ---------------------------------------------------------------------------------------

static char *mcp_op_layer_list(void) {
	slot_layer_t_array_t *ls = g_project->_->layers;
	mcp_begin_ok();
	mcp_kv_i("count", ls->length);
	mcp_kv_s("order", "index 0 is the BOTTOM of the stack; ArmorPaint's Layers panel lists the highest index first");
	mcp_kv_i("selected_index", g_context->layer == NULL ? -1 : array_index_of(ls, g_context->layer));
	mcp_open("layers", "[");
	for (int i = 0; i < ls->length; ++i) {
		mcp_emit_layer(ls->buffer[i], i);
	}
	mcp_close("]");
	return mcp_end_ok();
}

static char *mcp_op_layer_select(void) {
	char         *why = "";
	slot_layer_t *l   = mcp_target_layer(&why);
	if (l == NULL) {
		return mcp_fail("not_found", "%s", why);
	}
	context_set_layer(l);
	mcp_refresh_layers();
	return mcp_layer_ok(l);
}

static char *mcp_op_layer_new(void) {
	char *kind = mcp_arg("kind");
	if (kind == NULL || kind[0] == '\0') {
		kind = "paint";
	}
	slot_layer_t *l = g_context->layer;
	if (strcmp(kind, "paint") == 0) {
		l = layers_new_layer(true, -1, NULL);
		history_new_layer();
	}
	else if (strcmp(kind, "fill") == 0 || strcmp(kind, "decal") == 0) {
		// The UI queues this for the next frame (layers_create_fill_layer); doing it inline is
		// the same work, and lets the reply describe the new layer.
		l = layers_new_layer(false, -1, NULL);
		history_new_layer();
		l->uv_type     = strcmp(kind, "decal") == 0 ? UV_TYPE_PROJECT : UV_TYPE_UVMAP;
		l->object_mask = g_context->layer_filter;
		history_to_fill_layer();
		slot_layer_to_fill_layer(l);
	}
	else if (strcmp(kind, "group") == 0) {
		if (l == NULL) {
			return mcp_fail("no_project", "no layer is selected to group");
		}
		if (slot_layer_is_group(l) || slot_layer_is_in_group(l)) {
			return mcp_fail("bad_args", "groups cannot be nested: the selected layer is a group or already in one");
		}
		if (slot_layer_is_layer_mask(l)) {
			l = l->parent;
		}
		i32_map_t    *pointers = tab_layers_init_layer_map();
		slot_layer_t *group    = layers_new_group();
		context_set_layer(l);
		array_remove(g_project->_->layers, group);
		array_insert(g_project->_->layers, array_index_of(g_project->_->layers, l) + 1, group);
		l->parent = group;
		mcp_remap_pointers(pointers);
		context_set_layer(group);
		history_new_group();
		l = group;
	}
	else if (strcmp(kind, "black_mask") == 0 || strcmp(kind, "white_mask") == 0 || strcmp(kind, "fill_mask") == 0) {
		if (l == NULL) {
			return mcp_fail("no_project", "no layer is selected to mask");
		}
		if (slot_layer_is_mask(l) || slot_layer_is_filter(l)) {
			context_set_layer(l->parent);
		}
		l                       = g_context->layer;
		i32_map_t    *pointers = tab_layers_init_layer_map();
		slot_layer_t *m        = layers_new_mask(false, l, -1);
		mcp_remap_pointers(pointers);
		if (strcmp(kind, "black_mask") == 0) {
			slot_layer_clear(m, 0x00000000, NULL, 1.0, layers_default_rough, 0.0);
			history_new_black_mask();
		}
		else if (strcmp(kind, "white_mask") == 0) {
			slot_layer_clear(m, 0xffffffff, NULL, 1.0, layers_default_rough, 0.0);
			history_new_white_mask();
		}
		else {
			slot_layer_to_fill_layer(m);
			history_new_fill_mask();
		}
		g_context->layer_preview_dirty = true;
		layers_update_fill_layers();
		l = m;
	}
	else {
		return mcp_fail("bad_args", "unknown kind '%s'; expected paint, fill, decal, group, black_mask, white_mask or fill_mask", kind);
	}
	if (mcp_has("new_name") && l != NULL) {
		l->name = string_copy(mcp_arg("new_name"));
	}
	mcp_refresh_layers();
	return mcp_layer_ok(l);
}

static char *mcp_op_layer_delete(void) {
	char         *why = "";
	slot_layer_t *l   = mcp_target_layer(&why);
	if (l == NULL) {
		return mcp_fail("not_found", "%s", why);
	}
	if (!tab_layers_can_delete(l)) {
		return mcp_fail("bad_args", "ArmorPaint refuses to delete this layer (it is the last paint layer, or a group holding every layer)");
	}
	context_set_layer(l);
	tab_layers_delete_layer(l);
	mcp_refresh_layers();
	return mcp_layer_ok(g_context->layer);
}

static char *mcp_op_layer_duplicate(void) {
	char         *why = "";
	slot_layer_t *l   = mcp_target_layer(&why);
	if (l == NULL) {
		return mcp_fail("not_found", "%s", why);
	}
	context_set_layer(l);
	history_duplicate_layer();
	layers_duplicate_layer(l);
	mcp_refresh_layers();
	return mcp_layer_ok(g_context->layer);
}

static char *mcp_op_layer_set(void) {
	char         *why = "";
	slot_layer_t *l   = mcp_target_layer(&why);
	if (l == NULL) {
		return mcp_fail("not_found", "%s", why);
	}
	slot_layer_t *prev = g_context->layer;
	// Validate everything before touching anything, so a bad field changes nothing.
	int blend = -1;
	if (mcp_has("blending")) {
		char *b = mcp_arg("blending");
		for (int i = 0; i < MCP_BLEND_COUNT; ++i) {
			if (strcmp(b, mcp_blend_names[i]) == 0) {
				blend = i;
			}
		}
		if (blend < 0) {
			return mcp_fail("bad_args", "unknown blending '%s'", b);
		}
	}
	if (mcp_has("opacity")) {
		double o = mcp_arg_f("opacity", 1.0);
		if (o < 0.0 || o > 1.0) {
			return mcp_fail("bad_args", "opacity must be 0..1");
		}
	}
	if (mcp_has("new_name")) {
		char *prev_name = string_copy(l->name);
		l->name         = string_copy(mcp_arg("new_name"));
		history_layer_name(l, prev_name);
	}
	if (mcp_has("visible")) {
		bool v = mcp_arg_b("visible", true);
		if (v != l->visible) {
			history_layer_visible(l);
			l->visible = v;
		}
	}
	g_context->layer = l;
	if (mcp_has("opacity")) {
		history_layer_opacity();
		l->mask_opacity = (float)mcp_arg_f("opacity", 1.0);
	}
	if (blend >= 0) {
		history_layer_blending();
		l->blending = blend;
	}
	if (mcp_has("object_mask")) {
		history_layer_object();
		l->object_mask = mcp_arg_i("object_mask", 0);
		layers_set_object_mask();
	}
	if (mcp_has("scale")) {
		history_layer_scale();
		l->scale = (float)mcp_arg_f("scale", 1.0);
	}
	if (mcp_has("angle")) {
		history_layer_angle();
		l->angle = (float)mcp_arg_f("angle", 0.0);
	}
	g_context->layer = prev;
	if (l->fill_material != NULL) {
		layers_update_fill_layers();
	}
	mcp_refresh_layers();
	return mcp_layer_ok(l);
}

static char *mcp_op_layer_move(void) {
	char         *why = "";
	slot_layer_t *l   = mcp_target_layer(&why);
	if (l == NULL) {
		return mcp_fail("not_found", "%s", why);
	}
	int to = mcp_arg_i("to_index", -1);
	if (to < 0 || to >= g_project->_->layers->length) {
		return mcp_fail("bad_args", "to_index must be 0..%d", g_project->_->layers->length - 1);
	}
	if (!slot_layer_can_move(l, to)) {
		return mcp_fail("bad_args", "ArmorPaint does not allow this layer at index %d (groups cannot nest; masks and filters must sit above a layer)", to);
	}
	slot_layer_move(l, to);
	mcp_refresh_layers();
	return mcp_layer_ok(l);
}

// Context-menu actions, each exactly as tab_layers.c performs it.
static char *mcp_op_layer_action(void) {
	char *action = mcp_arg("action");
	if (action == NULL) {
		return mcp_fail("bad_args", "missing 'action'");
	}
	char         *why = "";
	slot_layer_t *l   = mcp_target_layer(&why);
	if (l == NULL) {
		return mcp_fail("not_found", "%s", why);
	}
	if (strcmp(action, "clear") == 0) {
		if (l->fill_material != NULL) {
			return mcp_fail("bad_args", "a fill layer cannot be cleared; convert it with to_paint first");
		}
		context_set_layer(l);
		if (!slot_layer_is_group(l)) {
			history_clear_layer();
			slot_layer_clear(l, slot_layer_is_mask(l) ? 0xffffffff : 0x00000000, NULL, 1.0, layers_default_rough, 0.0);
			util_layer_clear_path_points(l);
		}
		else {
			slot_layer_t_array_t *children = slot_layer_get_children(l);
			for (int i = 0; children != NULL && i < children->length; ++i) {
				slot_layer_t *c  = children->buffer[i];
				g_context->layer = c;
				history_clear_layer();
				slot_layer_clear(c, slot_layer_is_mask(c) ? 0xffffffff : 0x00000000, NULL, 1.0, layers_default_rough, 0.0);
			}
			g_context->layer = l;
		}
	}
	else if (strcmp(action, "merge_down") == 0) {
		if (!tab_layers_can_merge_down(l)) {
			return mcp_fail("bad_args", "this layer cannot be merged down (lowest layer, or the layer below is not compatible)");
		}
		context_set_layer(l);
		history_merge_layers();
		layers_merge_down();
		if (g_context->layer->fill_material != NULL) {
			slot_layer_to_paint_layer(g_context->layer);
		}
	}
	else if (strcmp(action, "merge_group") == 0) {
		if (!slot_layer_is_group(l)) {
			return mcp_fail("bad_args", "merge_group needs a group layer");
		}
		layers_merge_group(l);
	}
	else if (strcmp(action, "to_fill") == 0) {
		if (slot_layer_is_group(l) || l->fill_material != NULL) {
			return mcp_fail("bad_args", "the layer is a group or already a fill layer");
		}
		context_set_layer(l);
		slot_layer_is_layer(l) ? history_to_fill_layer() : history_to_fill_mask();
		slot_layer_to_fill_layer(l);
	}
	else if (strcmp(action, "to_paint") == 0) {
		if (slot_layer_is_group(l) || l->fill_material == NULL) {
			return mcp_fail("bad_args", "the layer is a group or already a paint layer");
		}
		context_set_layer(l);
		slot_layer_is_layer(l) ? history_to_paint_layer() : history_to_paint_mask();
		slot_layer_to_paint_layer(l);
	}
	else if (strcmp(action, "apply_mask") == 0) {
		if (!slot_layer_is_mask(l)) {
			return mcp_fail("bad_args", "apply_mask needs a mask");
		}
		g_context->layer = l;
		history_apply_mask();
		slot_layer_apply_mask(l);
		context_set_layer(l->parent);
		l = g_context->layer;
	}
	else if (strcmp(action, "invert_mask") == 0) {
		if (!slot_layer_is_mask(l) || l->fill_material != NULL) {
			return mcp_fail("bad_args", "invert_mask needs a paint mask");
		}
		context_set_layer(l);
		history_invert_mask();
		slot_layer_invert_mask(l);
	}
	else {
		return mcp_fail("bad_args", "unknown action '%s'; expected clear, merge_down, merge_group, to_fill, to_paint, apply_mask or invert_mask", action);
	}
	mcp_refresh_layers();
	return mcp_layer_ok(g_context->layer);
}

// ---- history --------------------------------------------------------------------------------------

static void mcp_emit_history(void) {
	mcp_kv_i("undos_available", history_undos);
	mcp_kv_i("redos_available", history_redos);
	mcp_kv_i("undo_steps_config", g_config->undo_steps);
	int active = history_steps->length - 1 - history_redos;
	mcp_open("steps", "[");
	int from = history_steps->length > 32 ? history_steps->length - 32 : 0;
	for (int i = from; i < history_steps->length; ++i) {
		history_step_t *s = history_steps->buffer[i];
		mcp_open(NULL, "{");
		mcp_kv_i("index", i);
		mcp_kv_s("name", s->name);
		mcp_kv_b("undone", i > active);
		mcp_close("}");
	}
	mcp_close("]");
}

static char *mcp_op_undo_redo(bool redo) {
	int n = mcp_arg_i("steps", 1);
	if (n < 1 || n > 64) {
		return mcp_fail("bad_args", "steps must be 1..64");
	}
	int done = 0;
	for (int i = 0; i < n; ++i) {
		if (redo ? history_redos <= 0 : history_undos <= 0) {
			break;
		}
		redo ? history_redo() : history_undo();
		done++;
	}
	g_context->ddirty = 2;
	mcp_begin_ok();
	mcp_kv_i(redo ? "redone" : "undone", done);
	mcp_emit_history();
	return mcp_end_ok();
}

static char *mcp_op_history(void) {
	mcp_begin_ok();
	mcp_emit_history();
	return mcp_end_ok();
}

// ---- export ---------------------------------------------------------------------------------------

static void mcp_fetch_presets(void) {
	if (box_export_files == NULL) {
		box_export_fetch_presets();
		i32 i                 = string_array_index_of(box_export_files, "generic");
		box_export_hpreset->i = i > 0 ? i : 0;
	}
}

static char *mcp_op_export_presets(void) {
	mcp_fetch_presets();
	mcp_begin_ok();
	mcp_open("presets", "[");
	for (int i = 0; i < box_export_files->length; ++i) {
		mcp_open(NULL, "");
		mcp_put_str(box_export_files->buffer[i]);
		mcp_first = 0;
	}
	mcp_close("]");
	mcp_kv_s("active", box_export_files->buffer[box_export_hpreset->i]);
	return mcp_end_ok();
}

static char *mcp_op_export_textures(void) {
	char *dir = mcp_arg("directory");
	if (dir == NULL || dir[0] == '\0') {
		return mcp_fail("bad_args", "missing 'directory'");
	}
	char *format = mcp_has("format") ? mcp_arg("format") : "png";
	int   bits   = mcp_arg_i("bits", -1);
	int   ftype  = 0;
	int   hbits  = base_bits_handle->i;
	if (strcmp(format, "png") == 0) {
		ftype = TEXTURE_LDR_FORMAT_PNG;
		hbits = TEXTURE_BITS_BITS8;
	}
	else if (strcmp(format, "jpg") == 0) {
		ftype = TEXTURE_LDR_FORMAT_JPG;
		hbits = TEXTURE_BITS_BITS8;
	}
	else if (strcmp(format, "exr") == 0) {
		ftype = g_context->format_type;
		hbits = bits == 32 ? TEXTURE_BITS_BITS32 : TEXTURE_BITS_BITS16;
	}
	else {
		return mcp_fail("bad_args", "format must be png, jpg or exr");
	}
	if (strcmp(format, "exr") != 0 && bits > 0 && bits != 8) {
		return mcp_fail("bad_args", "png and jpg are 8-bit; use format exr for 16 or 32 bits");
	}
	int layers_mode = g_context->layers_export;
	if (mcp_has("layers")) {
		char *lm = mcp_arg("layers");
		if (strcmp(lm, "visible") == 0) {
			layers_mode = EXPORT_MODE_VISIBLE;
		}
		else if (strcmp(lm, "selected") == 0) {
			layers_mode = EXPORT_MODE_SELECTED;
		}
		else if (strcmp(lm, "per_object") == 0) {
			layers_mode = EXPORT_MODE_PER_OBJECT;
		}
		else if (strcmp(lm, "per_udim_tile") == 0) {
			layers_mode = EXPORT_MODE_PER_UDIM_TILE;
		}
		else {
			return mcp_fail("bad_args", "layers must be visible, selected, per_object or per_udim_tile");
		}
	}
	mcp_fetch_presets();
	if (mcp_has("preset")) {
		i32 pi = string_array_index_of(box_export_files, mcp_arg("preset"));
		if (pi < 0) {
			return mcp_fail("not_found", "no export preset named '%s' (see export_presets)", mcp_arg("preset"));
		}
		if (pi != box_export_hpreset->i) {
			box_export_hpreset->i = pi;
			box_export_preset     = NULL;
		}
	}
	if (box_export_preset == NULL) {
		box_export_parse_preset();
	}
	// ArmorPaint exports at the layers' own bit depth, so a different depth means converting the
	// layers first -- exactly what the export dialog's "Color" combo does.
	bool converted = false;
	if (hbits != base_bits_handle->i) {
		base_bits_handle->i = hbits;
		layers_set_bits();
		converted = true;
	}
	g_context->format_type        = ftype;
	g_context->format_quality     = (float)mcp_arg_f("quality", g_context->format_quality);
	g_context->layers_export      = layers_mode;
	g_context->layers_destination = EXPORT_DESTINATION_DISK;
	if (mcp_has("filename")) {
		ui_files_filename = string_copy(mcp_arg("filename"));
	}
	iron_create_directory(dir);
	export_texture_run(dir, mcp_arg_b("bake_material", false));
	mcp_begin_ok();
	mcp_kv_s("directory", dir);
	mcp_kv_s("format", format);
	mcp_kv_i("bits", hbits == TEXTURE_BITS_BITS8 ? 8 : hbits == TEXTURE_BITS_BITS16 ? 16 : 32);
	mcp_kv_f("quality", g_context->format_quality);
	mcp_kv_s("preset", box_export_files->buffer[box_export_hpreset->i]);
	mcp_kv_i("layers_mode", layers_mode);
	mcp_kv_s("filename", string_equals(ui_files_filename, "") ? "untitled" : ui_files_filename);
	mcp_kv_b("layers_bit_depth_converted", converted);
	any_array_t *files = file_read_directory(dir);
	mcp_open("files", "[");
	for (int i = 0; files != NULL && i < files->length && i < 256; ++i) {
		mcp_open(NULL, "");
		mcp_put_str(files->buffer[i]);
		mcp_first = 0;
	}
	mcp_close("]");
	return mcp_end_ok();
}

// ---- bake -----------------------------------------------------------------------------------------

static const char *mcp_bake_names[] = {"curvature", "normal",   "normal_object", "height",   "derivative", "position",   "texcoord",
                                       "material_id", "object_id", "vertex_color",  "occlusion", "lightmap",  "bent_normal", "thickness"};
#define MCP_BAKE_COUNT 14

static void mcp_emit_bake_settings(void) {
	int t = (int)g_context->bake_type;
	mcp_kv_s("type", (t >= 0 && t < MCP_BAKE_COUNT) ? mcp_bake_names[t] : "none");
	mcp_kv_i("samples", g_context->bake_samples);
	mcp_kv_i("axis", (int)g_context->bake_axis);
	mcp_kv_i("up_axis", (int)g_context->bake_up_axis);
	mcp_kv_f("ao_strength", g_context->bake_ao_strength);
	mcp_kv_f("ao_radius", g_context->bake_ao_radius);
	mcp_kv_f("ao_offset", g_context->bake_ao_offset);
	mcp_kv_f("curv_strength", g_context->bake_curv_strength);
	mcp_kv_f("curv_radius", g_context->bake_curv_radius);
	mcp_kv_f("curv_offset", g_context->bake_curv_offset);
	mcp_kv_i("curv_smooth", g_context->bake_curv_smooth);
	mcp_kv_i("high_poly", g_context->bake_high_poly);
	mcp_kv_b("raytrace_supported", gpu_raytrace_supported());
	mcp_kv_b("baking", bake_texture_node_baking);
}

static void mcp_apply_bake_settings(void) {
	if (mcp_has("samples")) {
		g_context->bake_samples = mcp_arg_i("samples", g_context->bake_samples);
	}
	if (mcp_has("axis")) {
		g_context->bake_axis = mcp_arg_i("axis", 0);
	}
	if (mcp_has("up_axis")) {
		g_context->bake_up_axis = mcp_arg_i("up_axis", 0);
	}
	if (mcp_has("ao_strength")) {
		g_context->bake_ao_strength = (float)mcp_arg_f("ao_strength", 1.0);
	}
	if (mcp_has("ao_radius")) {
		g_context->bake_ao_radius = (float)mcp_arg_f("ao_radius", 1.0);
	}
	if (mcp_has("ao_offset")) {
		g_context->bake_ao_offset = (float)mcp_arg_f("ao_offset", 1.0);
	}
	if (mcp_has("curv_strength")) {
		g_context->bake_curv_strength = (float)mcp_arg_f("curv_strength", 1.0);
	}
	if (mcp_has("curv_radius")) {
		g_context->bake_curv_radius = (float)mcp_arg_f("curv_radius", 1.0);
	}
	if (mcp_has("curv_offset")) {
		g_context->bake_curv_offset = (float)mcp_arg_f("curv_offset", 0.0);
	}
	if (mcp_has("curv_smooth")) {
		g_context->bake_curv_smooth = mcp_arg_i("curv_smooth", 0);
	}
	if (mcp_has("high_poly")) {
		g_context->bake_high_poly = mcp_arg_i("high_poly", 0);
	}
}

static char *mcp_op_bake_settings(void) {
	mcp_apply_bake_settings();
	mcp_begin_ok();
	mcp_emit_bake_settings();
	return mcp_end_ok();
}

// Run a bake into a Bake Texture (TEX_BAKE) node, as its "Bake" button does
// (bake_texture_node_run). That function brackets a layer bit-depth change with
// draw_end()/draw_begin() because the button fires mid-draw; a request is handled in the update
// phase with no draw pass open, so the same steps run here without the bracket.
static char *mcp_op_bake(void) {
	if (bake_texture_node_baking) {
		return mcp_fail("app_busy", "a bake is already running; poll bake_status");
	}
	char *tname = mcp_arg("type");
	int   type  = -1;
	for (int i = 0; tname != NULL && i < MCP_BAKE_COUNT; ++i) {
		if (strcmp(tname, mcp_bake_names[i]) == 0) {
			type = i;
		}
	}
	if (type < 0) {
		return mcp_fail("bad_args", "unknown bake type; expected curvature, normal, normal_object, height, derivative, position, texcoord, material_id, object_id, vertex_color, occlusion, lightmap, bent_normal or thickness");
	}
	bool rt_bake = type == BAKE_TYPE_OCCLUSION || type == BAKE_TYPE_LIGHTMAP || type == BAKE_TYPE_BENT_NORMAL || type == BAKE_TYPE_THICKNESS;
	if (rt_bake && !gpu_raytrace_supported()) {
		return mcp_fail("unsupported", "%s needs hardware ray tracing, which this GPU/driver does not report", tname);
	}
	if (g_context->layer == NULL) {
		return mcp_fail("no_project", "no layer is selected");
	}
	int              node_id = mcp_arg_i("node_id", -1);
	ui_node_canvas_t *canvas = g_context->material != NULL ? g_context->material->canvas : NULL;
	ui_node_t        *node   = canvas != NULL ? ui_get_node(canvas->nodes, node_id) : NULL;
	if (node == NULL || !string_equals(node->type, "TEX_BAKE")) {
		return mcp_fail("bad_args", "node_id must name a TEX_BAKE node in the active material (add one with node_add type TEX_BAKE)");
	}
	mcp_apply_bake_settings();

	_bake_texture_node_tool = g_context->tool;
	char            *rt_name = string_tmp("bake_texture_node_%d", node->id);
	render_target_t *rt      = any_map_get(render_path_render_targets, rt_name);
	if (rt != NULL && rt->width != config_get_texture_res_x()) {
		gpu_delete_texture(rt->_image);
		rt->width  = config_get_texture_res_x();
		rt->height = config_get_texture_res_y();
		rt->_image = gpu_create_render_target(rt->width, rt->height, GPU_TEXTURE_FORMAT_RGBA32);
	}
	if (rt == NULL) {
		rt         = render_target_create();
		rt->name   = string_copy(rt_name);
		rt->width  = config_get_texture_res_x();
		rt->height = config_get_texture_res_y();
		rt->format = "RGBA32";
		render_path_create_render_target(rt);
	}
	if (g_context->viewport_mode == VIEWPORT_MODE_PATH_TRACE) {
		g_context->viewport_mode = VIEWPORT_MODE_LIT;
	}
	g_context->tool      = TOOL_TYPE_BAKE;
	g_context->bake_type = type;
	if (type == BAKE_TYPE_NORMAL || type == BAKE_TYPE_HEIGHT || type == BAKE_TYPE_DERIVATIVE) {
		gpu_delete_texture(rt->_image);
		rt->format              = "RGBA128";
		rt->_image              = gpu_create_render_target(rt->width, rt->height, GPU_TEXTURE_FORMAT_RGBA128);
		_bake_texture_node_bits = base_bits_handle->i;
		base_bits_handle->i     = TEXTURE_BITS_BITS32;
		layers_set_bits();
		history_push_undo = true;
	}
	i32 lid               = g_context->layer->id;
	_bake_texture_node_rt = any_map_get(render_path_render_targets, string_tmp("texpaint%d", lid));
	any_map_set(render_path_render_targets, string("texpaint%d", lid), rt);
	bake_texture_node_texpaint               = g_context->layer->texpaint;
	g_context->layer->texpaint               = rt->_image;
	g_context->pdirty                        = rt_bake ? g_context->bake_samples : 1;
	g_context->rtdirty                       = 1;
	render_path_raytrace_bake_current_sample = 0;
	render_path_raytrace_frame               = 0;
	bake_texture_node_baking                 = true;
	make_material_parse_paint_material(false);
	sys_notify_on_next_frame(bake_texture_node_clear, rt->_image);
	sys_notify_on_update(bake_texture_node_check_result, node);

	mcp_begin_ok();
	mcp_kv_i("node_id", node->id);
	mcp_kv_b("started", true);
	mcp_kv_s("note", "the bake runs over the next frames; poll bake_status until baking is false");
	mcp_emit_bake_settings();
	return mcp_end_ok();
}

static char *mcp_op_bake_status(void) {
	mcp_begin_ok();
	float progress = g_context->bake_samples > 0 ? render_path_raytrace_bake_current_sample / (float)g_context->bake_samples : 1.0f;
	mcp_kv_f("progress", bake_texture_node_baking ? (progress > 1.0f ? 1.0f : progress) : 1.0f);
	mcp_emit_bake_settings();
	return mcp_end_ok();
}

// ---- render settings (tone, LUT, post-processing) -------------------------------------------------

static void mcp_emit_render(void) {
	mcp_kv_f("ssao", g_config->rp_ssao);
	mcp_kv_f("bloom", g_config->rp_bloom);
	mcp_kv_f("contrast", g_config->rp_contrast);
	mcp_kv_f("gamma", g_config->rp_gamma);
	mcp_kv_f("vignette", g_config->rp_vignette);
	mcp_kv_f("grain", g_config->rp_grain);
	mcp_kv_f("supersample", g_config->rp_supersample);
	mcp_kv_s("lut_path", g_config->lut_path);
	mcp_kv_i("render_mode", (int)g_config->render_mode);
	mcp_kv_b("texture_filter", g_config->texture_filter);
	camera_object_t *cam = scene_camera;
	if (cam != NULL && cam->data != NULL) {
		mcp_kv_f("clip_start", cam->data->near_plane);
		mcp_kv_f("clip_end", cam->data->far_plane);
	}
}

static char *mcp_op_render_settings(void) {
	struct {
		const char *name;
		float      *field;
		double      lo;
		double      hi;
	} fs[] = {
	    {"ssao", &g_config->rp_ssao, 0.0, 1.0},         {"bloom", &g_config->rp_bloom, 0.0, 1.0},
	    {"contrast", &g_config->rp_contrast, 0.0, 2.0}, {"gamma", &g_config->rp_gamma, 0.0, 2.0},
	    {"vignette", &g_config->rp_vignette, 0.0, 1.0}, {"grain", &g_config->rp_grain, 0.0, 1.0},
	};
	int n = (int)(sizeof(fs) / sizeof(fs[0]));
	for (int i = 0; i < n; ++i) {
		if (mcp_has(fs[i].name)) {
			double v = mcp_arg_f(fs[i].name, 0.0);
			if (v < fs[i].lo || v > fs[i].hi) {
				return mcp_fail("bad_args", "%s must be %g..%g", fs[i].name, fs[i].lo, fs[i].hi);
			}
		}
	}
	bool changed = false;
	for (int i = 0; i < n; ++i) {
		if (mcp_has(fs[i].name)) {
			*fs[i].field = (float)mcp_arg_f(fs[i].name, 0.0);
			changed      = true;
		}
	}
	if (mcp_has("supersample")) {
		g_config->rp_supersample = (float)mcp_arg_f("supersample", 1.0);
		config_apply();
		changed = true;
	}
	if (mcp_has("render_mode")) {
		g_config->render_mode = mcp_arg_i("render_mode", 0);
		context_set_render_path();
		changed = true;
	}
	if (mcp_has("texture_filter")) {
		g_config->texture_filter = mcp_arg_b("texture_filter", true);
		gpu_use_linear_sampling(g_config->texture_filter);
		changed = true;
	}
	if (mcp_arg("lut_path") != NULL) {
		char *lut = mcp_arg("lut_path");
		if (lut[0] == '\0') {
			g_config->lut_path = "";
			import_lut_free();
		}
		else {
			if (!iron_file_exists(lut)) {
				return mcp_fail("not_found", "no such .cube file: %s", lut);
			}
			g_config->lut_path = string_copy(lut);
			box_preferences_lut_picked(g_config->lut_path);
		}
		changed = true;
	}
	camera_object_t *cam = scene_camera;
	if (cam != NULL && cam->data != NULL && (mcp_has("clip_start") || mcp_has("clip_end"))) {
		cam->data->near_plane = (float)mcp_arg_f("clip_start", cam->data->near_plane);
		cam->data->far_plane  = (float)mcp_arg_f("clip_end", cam->data->far_plane);
		camera_object_build_proj(cam, -1.0);
		changed = true;
	}
	if (changed) {
		g_context->ddirty = 2;
		config_save();
	}
	mcp_begin_ok();
	mcp_emit_render();
	return mcp_end_ok();
}

// ---- texture-set resolution -----------------------------------------------------------------------

static char *mcp_op_texture_resolution(void) {
	if (mcp_has("size")) {
		int size = mcp_arg_i("size", 0);
		int pos  = size == 2048 ? TEXTURE_RES_RES2048 : size == 4096 ? TEXTURE_RES_RES4096 : size == 8192 ? TEXTURE_RES_RES8192 : size == 16384 ? TEXTURE_RES_RES16384 : -1;
		if (pos < 0) {
			return mcp_fail("bad_args", "size must be 2048, 4096, 8192 or 16384");
		}
		base_res_handle->i = pos;
		config_set_texture_res(pos);
		layers_on_resized();
	}
	mcp_begin_ok();
	mcp_kv_i("width", config_get_texture_res_x());
	mcp_kv_i("height", config_get_texture_res_y());
	mcp_kv_s("note", "a resize is applied on the next frame (layers_on_resized)");
	return mcp_end_ok();
}

// ---- live project lists ---------------------------------------------------------------------------

static char *mcp_op_project_lists(void) {
	project_runtime_t *r = g_project->_;
	mcp_begin_ok();
	mcp_kv_b("live", true);
	mcp_open("materials", "[");
	for (int i = 0; r->materials != NULL && i < r->materials->length; ++i) {
		slot_material_t *m = r->materials->buffer[i];
		mcp_open(NULL, "{");
		mcp_kv_i("index", i);
		mcp_kv_i("id", m->id);
		mcp_kv_s("name", m->canvas != NULL ? m->canvas->name : NULL);
		mcp_kv_b("active", m == g_context->material);
		mcp_close("}");
	}
	mcp_close("]");
	mcp_open("textures", "[");
	for (int i = 0; r->assets != NULL && i < r->assets->length; ++i) {
		asset_t *a = r->assets->buffer[i];
		mcp_open(NULL, "{");
		mcp_kv_i("id", a->id);
		mcp_kv_s("name", a->name);
		mcp_kv_s("file", a->file);
		mcp_close("}");
	}
	mcp_close("]");
	mcp_open("brushes", "[");
	for (int i = 0; r->brushes != NULL && i < r->brushes->length; ++i) {
		slot_brush_t *b = r->brushes->buffer[i];
		mcp_open(NULL, "{");
		mcp_kv_i("index", i);
		mcp_kv_s("name", b->canvas != NULL ? b->canvas->name : NULL);
		mcp_kv_b("active", b == g_context->brush);
		mcp_close("}");
	}
	mcp_close("]");
	mcp_open("fonts", "[");
	for (int i = 0; r->fonts != NULL && i < r->fonts->length; ++i) {
		slot_font_t *f = r->fonts->buffer[i];
		mcp_open(NULL, "{");
		mcp_kv_s("name", f->name);
		mcp_kv_s("file", f->file);
		mcp_close("}");
	}
	mcp_close("]");
	mcp_open("paint_objects", "[");
	for (int i = 0; r->paint_objects != NULL && i < r->paint_objects->length; ++i) {
		mesh_object_t *o = r->paint_objects->buffer[i];
		mcp_open(NULL, "{");
		mcp_kv_i("index", i);
		mcp_kv_s("name", o->base != NULL ? o->base->name : NULL);
		mcp_kv_b("visible", o->base != NULL && o->base->visible);
		mcp_kv_b("active", o == g_context->paint_object);
		mcp_close("}");
	}
	mcp_close("]");
	mcp_kv_i("layer_count", r->layers->length);
	return mcp_end_ok();
}

// ---- camera ---------------------------------------------------------------------------------------

static char *mcp_op_camera(void) {
	camera_object_t *cam = scene_camera;
	if (cam == NULL) {
		return mcp_fail("internal", "no scene camera");
	}
	char *view = mcp_arg("view");
	if (view != NULL && view[0] != '\0') {
		f32 pi = math_pi();
		if (strcmp(view, "front") == 0) {
			viewport_set_view(0, -1, 0, pi / 2.0, 0, 0);
		}
		else if (strcmp(view, "back") == 0) {
			viewport_set_view(0, 1, 0, pi / 2.0, 0, pi);
		}
		else if (strcmp(view, "right") == 0) {
			viewport_set_view(1, 0, 0, pi / 2.0, 0, pi / 2.0);
		}
		else if (strcmp(view, "left") == 0) {
			viewport_set_view(-1, 0, 0, pi / 2.0, 0, -pi / 2.0);
		}
		else if (strcmp(view, "top") == 0) {
			viewport_set_view(0, 0, 1, 0, 0, 0);
		}
		else if (strcmp(view, "bottom") == 0) {
			viewport_set_view(0, 0, -1, pi, 0, pi);
		}
		else if (strcmp(view, "reset") == 0) {
			viewport_reset();
		}
		else {
			return mcp_fail("bad_args", "view must be front, back, left, right, top, bottom or reset");
		}
	}
	if (mcp_has("orbit_x") || mcp_has("orbit_y")) {
		viewport_orbit((float)mcp_arg_f("orbit_x", 0.0), (float)mcp_arg_f("orbit_y", 0.0));
	}
	if (mcp_has("zoom")) {
		viewport_zoom((float)mcp_arg_f("zoom", 0.0));
	}
	if (mcp_has("fov")) {
		cam->data->fov = (float)mcp_arg_f("fov", cam->data->fov);
		camera_object_build_proj(cam, -1.0);
	}
	g_context->ddirty = 2;
	transform_t *t    = cam->base->transform;
	mcp_begin_ok();
	mcp_open("location", "[");
	mcp_open(NULL, "");
	char tmp[96];
	snprintf(tmp, sizeof(tmp), "%.6g,%.6g,%.6g", t->loc.x, t->loc.y, t->loc.z);
	mcp_put(tmp);
	mcp_close("]");
	mcp_open("rotation_xyzw", "[");
	mcp_open(NULL, "");
	snprintf(tmp, sizeof(tmp), "%.6g,%.6g,%.6g,%.6g", t->rot.x, t->rot.y, t->rot.z, t->rot.w);
	mcp_put(tmp);
	mcp_close("]");
	mcp_kv_f("fov", cam->data->fov);
	return mcp_end_ok();
}

// ---- console --------------------------------------------------------------------------------------

// ArmorPaint keeps its last 100 console lines (console_log -> console_last_traces). Reading them
// back is how an agent sees a plugin's compile/run errors and its own console_write trail.
static char *mcp_op_console_read(void) {
	int max = mcp_arg_i("max_lines", 50);
	if (max < 1 || max > 100) {
		return mcp_fail("bad_args", "max_lines must be 1..100");
	}
	string_array_t *t = console_last_traces;
	int             n = t == NULL ? 0 : t->length;
	mcp_begin_ok();
	mcp_kv_i("available", n);
	mcp_open("lines", "[");
	for (int i = n > max ? n - max : 0; i < n; ++i) {
		mcp_open(NULL, "");
		mcp_put_str(t->buffer[i]);
		mcp_first = 0;
	}
	mcp_close("]");
	return mcp_end_ok();
}

// ---- entry point ----------------------------------------------------------------------------------

static const char *mcp_ops =
    "ext_info capture_viewport layer_list layer_select layer_new layer_delete layer_duplicate layer_set layer_move layer_action "
    "undo redo history export_presets export_textures_ex bake_settings bake bake_status render_settings texture_resolution "
    "project_lists camera console_read";

char *mcp_ext_call(char *op, any_map_t *args, char *prefix) {
	mcp_args   = args;
	mcp_prefix = prefix != NULL ? prefix : "";
	if (op == NULL) {
		return mcp_fail("bad_args", "missing op");
	}
	if (strcmp(op, "console_read") == 0) {
		return mcp_op_console_read(); // needs no project
	}
	if (strcmp(op, "ext_info") == 0) {
		mcp_begin_ok();
		mcp_kv_i("ext_version", MCP_EXT_VERSION);
		mcp_kv_s("ops", mcp_ops);
		mcp_kv_b("raytrace_supported", gpu_raytrace_supported());
		mcp_kv_s("sha", g_config->sha);
		return mcp_end_ok();
	}
	if (!mcp_have_project() || g_context == NULL) {
		return mcp_fail("no_project", "no project runtime");
	}
	if (strcmp(op, "capture_viewport") == 0) {
		return mcp_op_capture_viewport();
	}
	if (strcmp(op, "layer_list") == 0) {
		return mcp_op_layer_list();
	}
	if (strcmp(op, "layer_select") == 0) {
		return mcp_op_layer_select();
	}
	if (strcmp(op, "layer_new") == 0) {
		return mcp_op_layer_new();
	}
	if (strcmp(op, "layer_delete") == 0) {
		return mcp_op_layer_delete();
	}
	if (strcmp(op, "layer_duplicate") == 0) {
		return mcp_op_layer_duplicate();
	}
	if (strcmp(op, "layer_set") == 0) {
		return mcp_op_layer_set();
	}
	if (strcmp(op, "layer_move") == 0) {
		return mcp_op_layer_move();
	}
	if (strcmp(op, "layer_action") == 0) {
		return mcp_op_layer_action();
	}
	if (strcmp(op, "undo") == 0) {
		return mcp_op_undo_redo(false);
	}
	if (strcmp(op, "redo") == 0) {
		return mcp_op_undo_redo(true);
	}
	if (strcmp(op, "history") == 0) {
		return mcp_op_history();
	}
	if (strcmp(op, "export_presets") == 0) {
		return mcp_op_export_presets();
	}
	if (strcmp(op, "export_textures_ex") == 0) {
		return mcp_op_export_textures();
	}
	if (strcmp(op, "bake_settings") == 0) {
		return mcp_op_bake_settings();
	}
	if (strcmp(op, "bake") == 0) {
		return mcp_op_bake();
	}
	if (strcmp(op, "bake_status") == 0) {
		return mcp_op_bake_status();
	}
	if (strcmp(op, "render_settings") == 0) {
		return mcp_op_render_settings();
	}
	if (strcmp(op, "texture_resolution") == 0) {
		return mcp_op_texture_resolution();
	}
	if (strcmp(op, "project_lists") == 0) {
		return mcp_op_project_lists();
	}
	if (strcmp(op, "camera") == 0) {
		return mcp_op_camera();
	}
	return mcp_fail("unsupported", "unknown op: %s", op);
}
// --- armorpaint-mcp: end ---------------------------------------------------------------------------
