"""Read-only, permission-aware command navigation; clicks only prefill normal commands."""
import hashlib
import html
from urllib.parse import quote

from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import MessageChain

from .commands import collect_catalog
from .errors import V2Error
from .messaging.outbound import MARKDOWN_DENIED
from .settings import effective_layout


def node_token(node):
    return hashlib.sha256((node["id"] + (node.get("binding") or "")).encode()).hexdigest()[:16]


def escape_markdown(text):
    text = html.escape(str(text), quote=False)
    for char in "\\`*_{}[]()#+-!|":
        text = text.replace(char, "\\" + char)
    return text


def text_link(command, show, *, scene, enter=False):
    if enter and scene in {"group", "channel"}:
        raise V2Error("unsupported", "cmd-enter is not supported in groups/channels.")
    encoded, label = quote(command, safe=""), quote(show, safe="")
    if max(len(command), len(show), len(encoded), len(label)) > 100:
        return None
    if enter:
        return f'<qqbot-cmd-enter text="{html.escape(encoded, quote=True)}" />'
    return f'<qqbot-cmd-input text="{html.escape(encoded, quote=True)}" show="{html.escape(label, quote=True)}" reference="false" />'


def permitted(node, scene, admin):
    return node["enabled"] and (admin or not any(p == "admin" or scene in {"group", "channel"} and p in {"group_admin", "shared_group_admin"} for p in node["permission"]))


def render_help(catalog, settings, query="", *, admin=False, markdown=False):
    nodes = [n for n in catalog["nodes"] if permitted(n, catalog["scene"], admin)]
    menu = [n for n in nodes if n["menu_entry"]]
    if len(menu) != 1:
        raise V2Error("menu_unavailable", "The effective menu command is disabled or ambiguous.")
    prefix = menu[0]["command"]
    query = query.strip()
    args = query.split(maxsplit=2)
    mode = args[0] if args else "home"
    key, page = "", 0
    if mode in {"plugin", "group", "detail"}:
        key = args[1] if len(args) > 1 else ""
        if len(args) > 2:
            try:
                page = int(args[2])
            except ValueError:
                raise V2Error("invalid_menu_page", "Page must be an integer.") from None
    elif mode == "search":
        try:
            page = int(args[1]) if len(args) > 1 else 0
        except ValueError:
            raise V2Error("invalid_menu_page", "Use search <page> <words>.") from None
        key = args[2] if len(args) > 2 else ""
    elif mode in {"home", "system", "plugins"}:
        try:
            page = int(args[1]) if len(args) > 1 else 0
        except ValueError:
            raise V2Error("invalid_menu_page", "Page must be an integer.") from None
    else:
        raise V2Error("invalid_menu_query", "Use home/system/plugins/plugin/group/detail/search.")
    if len(query) > 256 or page < 0:
        raise V2Error("invalid_menu_page", "Menu query exceeds the supported range.")
    selection = settings["panels"][catalog["scene"]]
    selected = selection["selected"] if selection["mode"] == "custom" else [n["id"] for n in nodes if n["system"]]
    rank = {identity: i for i, identity in enumerate(selected)}
    ordered = sorted(nodes, key=lambda n: (rank.get(n["id"], len(rank)), n["plugin"], n["name"], n["id"]))
    entries, breadcrumb, layer, layout_node = [], [settings["title"]], "home", None
    def command(arg):
        return prefix + (" md " if markdown else " ") + arg
    def node_entry(node):
        token = node_token(node)
        target = "group" if node["group"] else "detail"
        desc = node["description"].splitlines()[0][:160] if node["description"] else "未提供说明"
        name = node["name"] if len(node["name"]) <= 512 else "长指令（请在控制台查看完整用法）"
        return {"id": node["id"], "label": name, "description": desc, "command": command(f"{target} {token} 0")}
    if mode == "home":
        entries = [{"id": "system", "label": "系统指令", "description": "适用于当前会话的系统命令", "command": command("system 0")},
                   {"id": "plugins", "label": "插件分类", "description": "按插件浏览，或使用 search 0 关键词", "command": command("plugins 0")}]
        entries += [node_entry(n) for n in ordered if n["id"] in rank and not n["menu_entry"]]
    elif mode == "plugins":
        plugins = sorted({n["plugin"] for n in nodes if not n["system"] and not n["menu_entry"]})
        entries = [{"id": name, "label": name, "description": "插件指令", "command": command("plugin " + hashlib.sha256(name.encode()).hexdigest()[:16] + " 0")} for name in plugins]
        breadcrumb.append("插件分类")
        layer = "plugin"
    elif mode == "system":
        entries = [node_entry(n) for n in ordered if n["system"]]
        breadcrumb.append("系统指令")
        layer = "plugin"
    elif mode == "plugin":
        plugins = {n["plugin"] for n in nodes if hashlib.sha256(n["plugin"].encode()).hexdigest()[:16] == key}
        if len(plugins) != 1:
            raise V2Error("menu_node_expired", "This plugin entry is no longer available.")
        plugin = next(iter(plugins))
        entries = [node_entry(n) for n in ordered if n["plugin"] == plugin and n["parent"] is None]
        breadcrumb += ["插件分类", plugin]
        layer, layout_node = "plugin", plugin
    elif mode in {"group", "detail"}:
        matched = [n for n in nodes if node_token(n) == key]
        if len(matched) != 1:
            raise V2Error("menu_node_expired", "Command source, parameters or permission changed; reopen the menu.")
        node = matched[0]
        breadcrumb += [node["plugin"], node["name"]]
        layer, layout_node = mode, node["id"]
        if mode == "group":
            entries = [node_entry(n) for n in ordered if n["parent"] == node["id"]]
        else:
            usage = node["usage"] if len(node["usage"]) <= 1024 else "用法超过文字展示上限；请在控制台查看，不提供截断命令。"
            entries.append({"id": node["id"], "label": usage, "description": node["description"][:320],
                            "command": node["command"] if len(node["command"]) <= 100 else None})
            for param in node["parameters"]:
                label = param["name"] + (" 必填" if param["required"] else " 可选") + " · " + param["type"]
                if param["default_visible"]:
                    label += " · 默认 " + str(param["default"])
                entries.append({"id": "param:" + param["name"], "label": label, "description": param["description"], "command": None})
    else:
        layer = "plugin"
        breadcrumb.append("搜索: " + key)
        entries = [node_entry(n) for n in ordered if key.casefold() in (n["name"] + " " + n["plugin"] + " " + n["description"]).casefold()]
    layout, inheritance = effective_layout(settings, catalog["scene"], layer, layout_node)
    chunks, chunk, size = [], [], 0
    for entry in entries:
        weight = len(entry["label"]) + len(entry["description"]) + len(entry["command"] or "") * 3 + 120
        if chunk and (len(chunk) >= min(layout["page_size"], 5 * layout["columns"] - 4) or size + weight > 2400):
            chunks.append(chunk)
            chunk, size = [], 0
        chunk.append(entry)
        size += weight
    chunks.append(chunk)
    if page >= len(chunks):
        raise V2Error("invalid_menu_page", "Page is outside the available results.")
    title = " > ".join(breadcrumb)
    text_title = title if len(title) <= 512 else settings["title"] + " > 长指令详情"
    lines = [escape_markdown(text_title) if markdown else text_title]
    if markdown and layout["style"] == "heading":
        lines[0] = "## " + lines[0]
    elif markdown and layout["style"] == "quote":
        lines[0] = "> " + lines[0]
    for entry in chunks[page]:
        label, desc = entry["label"], entry["description"]
        link = text_link(entry["command"], label, scene=catalog["scene"]) if markdown and entry["command"] else None
        lines.append(link or (escape_markdown(label) if markdown else label))
        if layout["show_description"] and desc:
            lines.append(escape_markdown(desc) if markdown else desc)
        if entry["command"] and not link:
            lines.append("输入：" + (escape_markdown(entry["command"]) if markdown else entry["command"]))
    def page_arg(number):
        if mode == "search":
            return f"search {number} {key}"
        return f"{mode} {key} {number}" if mode in {"plugin", "group", "detail"} else f"{mode} {number}"
    nav = [("首页", command("home 0"))]
    if mode in {"group", "detail"}:
        node = next(n for n in nodes if node_token(n) == key)
        parent = next((n for n in nodes if n["id"] == node["parent"]), None)
        nav.append(("返回上级", command("group " + node_token(parent) + " 0") if parent else command("plugins 0")))
    if page:
        nav.append(("上一页", command(page_arg(page - 1))))
    if page + 1 < len(chunks):
        nav.append(("下一页", command(page_arg(page + 1))))
    lines += [f"第{page + 1}/{len(chunks)}页 · {len(entries)}项"]
    for label, invocation in nav:
        lines.append((text_link(invocation, label, scene=catalog["scene"]) if markdown else None) or f"{label}：{invocation}")
    if catalog["scene"] in {"group", "channel"}:
        lines.append("仅收@的场景请真实@机器人后发送；点击只预填，不会直接执行。")
    if mode == "detail":
        lines.append("参数只作提示/预填；权限和参数仍由正常指令管线校验。")
    body = "\n".join(lines)
    if len(body) > 4096 or len(body.encode()) > 16384:
        raise V2Error("menu_too_large", "Menu text exceeds the basic send limit; reduce page size or description.")
    return {"text": body, "page": page, "pages": len(chunks), "total": len(entries), "items": chunks[page],
            "breadcrumb": breadcrumb, "layout": layout, "inheritance": inheritance, "markdown": markdown}


async def send_help(owner, event, query):
    markdown = query == "md" or query.startswith("md ")
    if markdown:
        query = query[2:].strip()
    config = owner.context.get_config(event.unified_msg_origin)
    catalog = collect_catalog(config, event.route.scene)
    settings = owner.store.get(event.bot.identity.settings_key)["applied"]
    settings = owner.panels.filter_pins(event.bot, settings, catalog)
    page = render_help(catalog, settings, query, admin=event.is_admin(), markdown=markdown)
    try:
        await event.send(MessageChain([Plain(page["text"])]).use_markdown(markdown))
    except V2Error as exc:
        if not markdown or exc.phase != "rejected" or exc.business_code not in MARKDOWN_DENIED:
            raise
        page = render_help(catalog, settings, query, admin=event.is_admin(), markdown=False)
        await event.send(MessageChain([Plain("Markdown权限被拒绝，改用纯文本帮助。\n" + page["text"])]).use_markdown(False))
