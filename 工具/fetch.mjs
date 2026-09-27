/**
 * 下载构建索引需要的原始词库文件到 工具\下载\。
 *
 * 本机没有可用的 curl / Invoke-WebRequest，统一走 Node 的 fetch。
 *
 * 用法（PowerShell）：
 *     $node = "C:\Users\creep\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe"
 *     & $node "工具\fetch.mjs"                # 下载全部
 *     & $node "工具\fetch.mjs" ecdict.csv     # 只下载指定文件
 *     & $node "工具\fetch.mjs" --check        # 只探测地址是否可用，不落盘
 */

import { createHash } from "node:crypto";
import { mkdir, stat, writeFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const OUT_DIR = join(HERE, "下载");

/** name -> [候选地址…]，按顺序尝试，第一个成功的就用。 */
const TARGETS = {
  "ecdict.csv": [
    "https://raw.githubusercontent.com/skywind3000/ECDICT/master/ecdict.csv",
    "https://raw.githubusercontent.com/skywind3000/ECDICT/main/ecdict.csv",
  ],
  "lemma.en.txt": [
    "https://raw.githubusercontent.com/skywind3000/ECDICT/master/lemma.en.txt",
    "https://raw.githubusercontent.com/skywind3000/ECDICT/main/lemma.en.txt",
  ],
  "n1.csv": [
    "https://raw.githubusercontent.com/jamsinclair/open-anki-jlpt-decks/main/src/n1.csv",
  ],
  "n2.csv": [
    "https://raw.githubusercontent.com/jamsinclair/open-anki-jlpt-decks/main/src/n2.csv",
  ],
  "n3.csv": [
    "https://raw.githubusercontent.com/jamsinclair/open-anki-jlpt-decks/main/src/n3.csv",
  ],
  "n4.csv": [
    "https://raw.githubusercontent.com/jamsinclair/open-anki-jlpt-decks/main/src/n4.csv",
  ],
  "n5.csv": [
    "https://raw.githubusercontent.com/jamsinclair/open-anki-jlpt-decks/main/src/n5.csv",
  ],
  // v0.3 新增：英语六本词书（wordforge）与日语两套公开表（eggrolls / OpenJLPT）
  "wordforge_words.json": [
    "https://raw.githubusercontent.com/zhiyf-huajq/wordforge/main/build/data_words.json",
  ],
  "wordforge_books.json": [
    "https://raw.githubusercontent.com/zhiyf-huajq/wordforge/main/build/data_books.json",
  ],
  "eggrolls_notes.csv": [
    "https://raw.githubusercontent.com/5mdld/anki-jlpt-decks/main/deck-source/notes.csv",
  ],
  "openjlpt-n5.csv": [
    "https://raw.githubusercontent.com/evanclan/OpenJLPT/main/data/csv/vocab-n5.csv",
  ],
  "openjlpt-n4.csv": [
    "https://raw.githubusercontent.com/evanclan/OpenJLPT/main/data/csv/vocab-n4.csv",
  ],
  "openjlpt-n3.csv": [
    "https://raw.githubusercontent.com/evanclan/OpenJLPT/main/data/csv/vocab-n3.csv",
  ],
  "openjlpt-n2.csv": [
    "https://raw.githubusercontent.com/evanclan/OpenJLPT/main/data/csv/vocab-n2.csv",
  ],
  "openjlpt-n1.csv": [
    "https://raw.githubusercontent.com/evanclan/OpenJLPT/main/data/csv/vocab-n1.csv",
  ],
  // Tatoeba 英语例句（构建期一次性抓，bzip2 压缩，解压后按词表筛）
  "tatoeba_eng_sentences.tsv.bz2": [
    "https://downloads.tatoeba.org/exports/per_language/eng/eng_sentences.tsv.bz2",
  ],
};

async function download(url, dest) {
  const res = await fetch(url, {
    redirect: "follow",
    headers: { "user-agent": "Mozilla/5.0 (anki-exam-vocab-index-builder)" },
  });
  if (!res.ok) {
    throw new Error(`HTTP ${res.status} ${res.statusText}`);
  }
  const buf = Buffer.from(await res.arrayBuffer());
  await writeFile(dest, buf);
  const sha = createHash("sha256").update(buf).digest("hex").slice(0, 16);
  return { bytes: buf.length, sha };
}

async function head(url) {
  const res = await fetch(url, {
    method: "GET",
    redirect: "follow",
    headers: { range: "bytes=0-64", "user-agent": "Mozilla/5.0 (anki-exam-vocab-index-builder)" },
  });
  const body = await res.text();
  return { status: res.status, preview: body.slice(0, 60).replace(/\s+/g, " ") };
}

async function main() {
  const argv = process.argv.slice(2);
  const checkOnly = argv.includes("--check");
  const names = argv.filter((a) => !a.startsWith("--"));
  const wanted = names.length ? names : Object.keys(TARGETS);

  const unknown = wanted.filter((n) => !TARGETS[n]);
  if (unknown.length) {
    console.error(`未知文件：${unknown.join("、")}`);
    console.error(`可选：${Object.keys(TARGETS).join("、")}`);
    return 2;
  }

  await mkdir(OUT_DIR, { recursive: true });
  let failed = 0;
  for (const name of wanted) {
    if (checkOnly) {
      for (const url of TARGETS[name]) {
        try {
          const info = await head(url);
          console.log(`[check] ${name} <- ${url} : HTTP ${info.status} :: ${info.preview}`);
          break;
        } catch (err) {
          console.log(`[check] ${name} <- ${url} : 失败 ${err.message}`);
        }
      }
      continue;
    }

    const dest = join(OUT_DIR, name);
    let done = false;
    for (const url of TARGETS[name]) {
      const started = Date.now();
      try {
        const info = await download(url, dest);
        const size = (info.bytes / 1024 / 1024).toFixed(2);
        console.log(
          `[ok] ${name} <- ${url} : ${size} MB，sha256:${info.sha}…，${(
            (Date.now() - started) / 1000
          ).toFixed(1)}s`
        );
        done = true;
        break;
      } catch (err) {
        console.log(`[跳过] ${name} <- ${url} : ${err.message}`);
      }
    }
    if (!done) {
      failed += 1;
      console.error(`[失败] ${name} 的所有候选地址都不可用`);
    } else {
      const st = await stat(dest);
      if (st.size < 1024) {
        console.error(`[可疑] ${name} 只有 ${st.size} 字节，可能不是真实文件`);
        failed += 1;
      }
    }
  }
  console.log(failed ? `结束：${failed} 个文件失败` : "结束：全部成功");
  return failed ? 1 : 0;
}

process.exitCode = await main();
