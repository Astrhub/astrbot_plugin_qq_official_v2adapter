import fs from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";
import { fileURLToPath, pathToFileURL } from "node:url";
import { applyHostContext, t, tState } from "../src/i18n.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const rootDir = path.resolve(__dirname, "../..");
const htmlPath = path.join(rootDir, "pages/control/index.html");
const outDir = path.join(rootDir, "docs/assets");
const pcSvgPath = path.join(outDir, "vue-control-preview-pc.svg");
const mobileSvgPath = path.join(outDir, "vue-control-preview-mobile.svg");

applyHostContext({ locale: "zh-CN", isDark: false });

const REQUIRED_TOKENS = [
  t("page.title"),
  t("nav.connection"),
  t("nav.commands"),
  t("nav.remote"),
  t("nav.sdk"),
  t("conn.statusTitle"),
  t("conn.gatewayTitle"),
  t("conn.scanTitle"),
  t("conn.formTitle"),
  t("conn.saveBtn"),
  tState("online"),
];

function findPlaywrightAndShell() {
  const npxRoot = "/root/.npm/_npx";
  const cacheRoot = "/root/.cache/ms-playwright";
  let pwPath = null;
  let shellPath = null;
  if (fs.existsSync(npxRoot)) {
    for (const d of fs.readdirSync(npxRoot)) {
      const candidate = path.join(npxRoot, d, "node_modules/playwright");
      if (fs.existsSync(candidate)) {
        pwPath = candidate;
        break;
      }
    }
  }
  if (fs.existsSync(cacheRoot)) {
    for (const d of fs.readdirSync(cacheRoot)) {
      if (d.startsWith("chromium_headless_shell")) {
        const candidate = path.join(cacheRoot, d, "chrome-headless-shell-linux64/chrome-headless-shell");
        if (fs.existsSync(candidate)) {
          shellPath = candidate;
          break;
        }
      }
    }
  }
  return { pwPath, shellPath };
}

async function captureDomToSvg(page, width, height, titleText) {
  await page.setViewportSize({ width, height });
  await page.waitForTimeout(80);
  const fullHeight = await page.evaluate((minH) => {
    const app = document.querySelector("#app");
    return Math.max(minH, Math.ceil(app.scrollHeight));
  }, height);
  if (fullHeight !== height) {
    await page.setViewportSize({ width, height: fullHeight });
    await page.waitForTimeout(50);
  }

  return page.evaluate(({ vw, vh, title }) => {
    const esc = (s) =>
      String(s)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;");

    const isTransparent = (c) => !c || c === "transparent" || c === "rgba(0, 0, 0, 0)";
    const num = (v) => parseFloat(v || "0") || 0;
    const out = [];

    function walkElement(el) {
      if (!(el instanceof Element)) return;
      const cs = getComputedStyle(el);
      if (cs.display === "none" || cs.visibility === "hidden" || num(cs.opacity) === 0) return;
      const rect = el.getBoundingClientRect();
      if (rect.width <= 0.5 || rect.height <= 0.5) return;

      const x = +rect.left.toFixed(1);
      const y = +rect.top.toFixed(1);
      const w = +rect.width.toFixed(1);
      const h = +rect.height.toFixed(1);
      const rx = Math.min(w / 2, h / 2, +num(cs.borderTopLeftRadius).toFixed(1));

      if (el instanceof SVGSVGElement) {
        const vb = el.getAttribute("viewBox") || `0 0 ${w} ${h}`;
        const sw = el.getAttribute("stroke-width") || "1.8";
        out.push(
          `<svg x="${x}" y="${y}" width="${w}" height="${h}" viewBox="${esc(vb)}" fill="none" stroke="${esc(cs.color)}" stroke-width="${esc(sw)}">${el.innerHTML}</svg>`
        );
        return;
      }

      if (el instanceof HTMLInputElement && el.type === "checkbox") {
        if (el.checked) {
          out.push(`<rect x="${x}" y="${y}" width="${w}" height="${h}" rx="4" fill="#3c96ca"/>`);
          const cx = x + w * 0.22;
          const cy = y + h * 0.52;
          out.push(
            `<path d="M${cx.toFixed(1)} ${cy.toFixed(1)} L${(x + w * 0.44).toFixed(1)} ${(y + h * 0.72).toFixed(1)} L${(x + w * 0.78).toFixed(1)} ${(y + h * 0.3).toFixed(1)}" fill="none" stroke="#ffffff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>`
          );
        } else {
          out.push(`<rect x="${x}" y="${y}" width="${w}" height="${h}" rx="4" fill="#ffffff" stroke="#cbd5e1" stroke-width="1.2"/>`);
        }
        return;
      }

      const bg = cs.backgroundColor;
      const bt = num(cs.borderTopWidth);
      const br = num(cs.borderRightWidth);
      const bb = num(cs.borderBottomWidth);
      const bl = num(cs.borderLeftWidth);
      const uniformBorder = bt > 0 && bt === br && bt === bb && bt === bl && !isTransparent(cs.borderTopColor);

      if (el.tagName === "LEGEND" && isTransparent(bg)) {
        out.push(`<rect x="${x}" y="${y}" width="${w}" height="${h}" fill="#f8fafc"/>`);
      } else if (!isTransparent(bg) || uniformBorder) {
        const fill = isTransparent(bg) ? "none" : bg;
        const strokeAttr = uniformBorder ? ` stroke="${esc(cs.borderTopColor)}" stroke-width="${bt}"` : "";
        const rxAttr = rx > 0 ? ` rx="${rx}"` : "";
        out.push(`<rect x="${x}" y="${y}" width="${w}" height="${h}"${rxAttr} fill="${esc(fill)}"${strokeAttr}/>`);
      }

      if (!uniformBorder) {
        if (bt > 0 && !isTransparent(cs.borderTopColor)) {
          out.push(`<line x1="${x}" y1="${y}" x2="${x + w}" y2="${y}" stroke="${esc(cs.borderTopColor)}" stroke-width="${bt}"/>`);
        }
        if (br > 0 && !isTransparent(cs.borderRightColor)) {
          out.push(`<line x1="${x + w}" y1="${y}" x2="${x + w}" y2="${y + h}" stroke="${esc(cs.borderRightColor)}" stroke-width="${br}"/>`);
        }
        if (bb > 0 && !isTransparent(cs.borderBottomColor)) {
          out.push(`<line x1="${x}" y1="${y + h}" x2="${x + w}" y2="${y + h}" stroke="${esc(cs.borderBottomColor)}" stroke-width="${bb}"/>`);
        }
        if (bl > 0 && !isTransparent(cs.borderLeftColor)) {
          out.push(`<line x1="${x}" y1="${y}" x2="${x}" y2="${y + h}" stroke="${esc(cs.borderLeftColor)}" stroke-width="${bl}"/>`);
        }
      }

      const before = getComputedStyle(el, "::before");
      if (before && before.content && before.content !== "none" && !isTransparent(before.backgroundColor)) {
        const bw = num(before.width);
        const bh = h - num(before.top) - num(before.bottom);
        if (bw > 0 && bh > 0) {
          const bx = x + num(before.left);
          const by = y + num(before.top);
          out.push(`<rect x="${+bx.toFixed(1)}" y="${+by.toFixed(1)}" width="${+bw.toFixed(1)}" height="${+bh.toFixed(1)}" rx="1.5" fill="${esc(before.backgroundColor)}"/>`);
        }
      }

      if (el instanceof HTMLInputElement && el.type !== "checkbox") {
        const val = el.value || el.placeholder || "";
        if (val) {
          const padL = num(cs.paddingLeft) || 10;
          const fs = num(cs.fontSize) || 13.5;
          const color = el.value ? cs.color : "rgba(27, 28, 29, 0.45)";
          const ty = +(y + h / 2 + fs * 0.35).toFixed(1);
          out.push(
            `<text x="${+(x + padL).toFixed(1)}" y="${ty}" font-size="${fs}" font-weight="${esc(cs.fontWeight)}" fill="${esc(color)}">${esc(val)}</text>`
          );
        }
        return;
      }

      for (const child of el.childNodes) {
        if (child.nodeType === Node.ELEMENT_NODE) {
          walkElement(child);
        } else if (child.nodeType === Node.TEXT_NODE) {
          const raw = child.nodeValue || "";
          if (!raw.trim()) continue;
          const range = document.createRange();
          range.selectNodeContents(child);
          const r = range.getBoundingClientRect();
          if (r.width <= 0.5 || r.height <= 0.5) continue;
          const fs = num(cs.fontSize) || 14;
          const fw = cs.fontWeight || "500";
          const isMono = cs.fontFamily.includes("monospace");
          const ffAttr = isMono ? ` font-family="ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"` : "";
          const ty = +(r.top + r.height * 0.76).toFixed(1);
          out.push(
            `<text x="${+r.left.toFixed(1)}" y="${ty}" font-size="${+fs.toFixed(1)}" font-weight="${esc(fw)}" fill="${esc(cs.color)}"${ffAttr}>${esc(raw.trim())}</text>`
          );
        }
      }
    }

    walkElement(document.querySelector("#app"));

    return `<svg xmlns="http://www.w3.org/2000/svg" width="${vw}" height="${vh}" viewBox="0 0 ${vw} ${vh}" role="img" aria-label="${esc(title)}">
  <title>${esc(title)}</title>
  <style>
    text { font-family: "Outfit", "Noto Sans", "PingFang SC", "Noto Sans CJK SC", "Microsoft YaHei", sans-serif; }
  </style>
  <rect width="${vw}" height="${vh}" rx="10" fill="#ffffff"/>
  ${out.join("\n  ")}
  <rect x="0.5" y="0.5" width="${vw - 1}" height="${vh - 1}" rx="10" fill="none" stroke="#dbe2ea"/>
</svg>\n`;
  }, { vw: width, vh: fullHeight, title: titleText });
}

async function main() {
  const { pwPath, shellPath } = findPlaywrightAndShell();
  if (!pwPath || !shellPath) {
    for (const svgPath of [pcSvgPath, mobileSvgPath]) {
      if (!fs.existsSync(svgPath)) {
        throw new Error(`Missing preview SVG ${svgPath} and no headless Chromium available to generate it.`);
      }
      const content = fs.readFileSync(svgPath, "utf8");
      for (const token of REQUIRED_TOKENS) {
        if (!content.includes(token)) {
          throw new Error(`Preview SVG ${path.basename(svgPath)} is out of sync (missing "${token}").`);
        }
      }
    }
    return;
  }

  const require = createRequire(import.meta.url);
  const { chromium } = require(pwPath);
  const browser = await chromium.launch({ executablePath: shellPath });
  try {
    const page = await browser.newPage();
    await page.addInitScript(() => {
      window.AstrBotPluginView = {
        ready: async () => ({ locale: "zh-CN", isDark: false }),
        getContext: () => ({ locale: "zh-CN", isDark: false }),
        getLocale: () => "zh-CN",
        t: (_k, fb) => fb,
        onContext: () => () => {},
        apiGet: async (ep) => {
          if (ep === "bootstrap") {
            return {
              csrf: "demo",
              flags: { remote_menu_sync: false },
              flags_revision: "1",
              instances: [{ id: "qq_v2", appid: "102030405" }],
            };
          }
          if (ep === "connection") {
            return {
              platform_id: "qq_v2",
              exists: true,
              fingerprint: "fp1",
              fields: {
                appid: "102030405",
                use_markdown: true,
                type: "qq_official_v2",
                intents: 1174409216,
                shard_mode: "auto",
                shard: [0, 1],
                enable: true,
              },
              credentials_configured: true,
              runtime_state: "online",
              runtime: {
                state: "online",
                message_ready: true,
                gateway_group: {
                  state: "online",
                  recommended: 2,
                  planned: 2,
                  connected: 2,
                  shards: [
                    { index: 0, count: 2, state: "online" },
                    { index: 1, count: 2, state: "online" },
                  ],
                },
              },
              capabilities: { sdk: { native_implemented: [] }, actions: {} },
            };
          }
          if (ep === "config") {
            return {
              platform_id: "qq_v2",
              revision: 1,
              fingerprint: "cfp",
              versions: [1],
              draft: {
                title: "菜单",
                panels: { group: { mode: "default", selected: [] } },
                layout: {
                  home: { page_size: 6, columns: 2, style: "plain", show_description: true },
                  plugin: { page_size: 6, columns: 2, style: "plain", show_description: true },
                  group: { page_size: 6, columns: 2, style: "plain", show_description: true },
                  detail: { page_size: 6, columns: 2, style: "plain", show_description: true },
                },
                scene_overrides: {},
                node_overrides: {},
                extensions: {},
              },
              remote_state: {},
            };
          }
          if (ep === "commands") return { nodes: [] };
          return {};
        },
        apiPost: async () => ({}),
      };
    });

    await page.goto(pathToFileURL(htmlPath).href, { waitUntil: "networkidle" });
    const pcSvg = await captureDomToSvg(page, 1360, 760, `${t("page.title")} · 桌面预览`);
    const mobileSvg = await captureDomToSvg(page, 680, 1180, `${t("page.title")} · 移动端预览`);

    fs.mkdirSync(outDir, { recursive: true });
    fs.writeFileSync(pcSvgPath, pcSvg, "utf8");
    fs.writeFileSync(mobileSvgPath, mobileSvg, "utf8");
  } finally {
    await browser.close();
  }
}

await main();
