// 通过 GitHub 官方 Git Data API 把本地 HEAD 的内容推成一个提交。
//
// 为什么要这么绕：本机网络屏蔽了 github.com:443（TCP 直接超时），但
// api.github.com / uploads.github.com / raw.githubusercontent.com 是通的，
// 所以 `git push` 走不通，只能走 API 建 blob → tree → commit → ref。
//
// 用法（token 从环境变量读，不落到磁盘、不打印）：
//   $env:GH_TOKEN = (& gh auth token); node 工具\push_via_api.mjs

import { execFileSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";

const OWNER = "creeperboo";
const REPO = "anki-exam-vocab-stats";
const BRANCH = "main";
const MESSAGE =
  process.env.COMMIT_MESSAGE ||
  "应试词汇覆盖统计 v0.3.0：修浏览器崩溃 + 素材库本地 zip 通道 + 钩子签名防护";

const token = process.env.GH_TOKEN;
if (!token) {
  console.error("缺少 GH_TOKEN 环境变量");
  process.exit(2);
}

const ROOT = process.cwd();
const API = `https://api.github.com/repos/${OWNER}/${REPO}`;
const HEADERS = {
  Authorization: `Bearer ${token}`,
  Accept: "application/vnd.github+json",
  "X-GitHub-Api-Version": "2022-11-28",
  "User-Agent": "exam-vocab-stats-push",
  "Content-Type": "application/json",
};

async function call(method, url, body) {
  const res = await fetch(url, {
    method,
    headers: HEADERS,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await res.text();
  if (!res.ok) {
    throw new Error(`${method} ${url} -> ${res.status} ${text.slice(0, 500)}`);
  }
  return text ? JSON.parse(text) : {};
}

// core.quotepath=false：否则中文路径会被 git 输出成 "\346\200..." 的转义写法
const files = execFileSync(
  "git",
  ["-c", "core.quotepath=false", "ls-tree", "-r", "--name-only", "HEAD"],
  { cwd: ROOT, encoding: "utf8" },
)
  .split(/\r?\n/)
  .filter(Boolean);

// 空仓库不能直接用 Git Data API（会回 409 "Git Repository is empty."），
// 先用 Contents API 落一个初始提交，之后再用 blob/tree/commit 一次性提交全部文件。
async function ensureNotBare() {
  try {
    await call("GET", `${API}/git/ref/heads/${BRANCH}`);
    return;
  } catch (err) {
    // 空仓库时远端回的是 409 "Git Repository is empty."，分支不存在时是 404
    const msg = String(err.message);
    if (!msg.includes("404") && !msg.includes("409")) throw err;
  }
  const seed = ".gitignore";
  const buf = fs.readFileSync(path.join(ROOT, seed));
  await call("PUT", `${API}/contents/${seed}`, {
    message: "初始化仓库",
    content: buf.toString("base64"),
    branch: BRANCH,
  });
  console.log("空仓库：已用 Contents API 落一个初始提交，接着提交全部文件。");
}

await ensureNotBare();

console.log(`本地 HEAD 共 ${files.length} 个文件，开始逐个上传 blob…`);

const tree = [];
let done = 0;
for (const rel of files) {
  const full = path.join(ROOT, ...rel.split("/"));
  const buf = fs.readFileSync(full);
  const blob = await call("POST", `${API}/git/blobs`, {
    content: buf.toString("base64"),
    encoding: "base64",
  });
  tree.push({ path: rel, mode: "100644", type: "blob", sha: blob.sha });
  done += 1;
  if (done % 10 === 0 || done === files.length) {
    console.log(`  已上传 ${done}/${files.length}`);
  }
}

const newTree = await call("POST", `${API}/git/trees`, { tree });
console.log(`已建 tree：${newTree.sha}`);

let parent = null;
try {
  const ref = await call("GET", `${API}/git/ref/heads/${BRANCH}`);
  parent = ref.object.sha;
} catch (err) {
  if (!String(err.message).includes("404")) throw err;
  console.log(`远端还没有 ${BRANCH} 分支，按首次提交处理。`);
}

const commitBody = { message: MESSAGE, tree: newTree.sha };
if (parent) commitBody.parents = [parent];
const commit = await call("POST", `${API}/git/commits`, commitBody);
console.log(`已建 commit：${commit.sha}`);

if (parent) {
  await call("PATCH", `${API}/git/refs/heads/${BRANCH}`, {
    sha: commit.sha,
    force: false,
  });
  console.log(`已把 ${BRANCH} 指向新提交。`);
} else {
  await call("POST", `${API}/git/refs`, {
    ref: `refs/heads/${BRANCH}`,
    sha: commit.sha,
  });
  console.log(`已创建分支 ${BRANCH}。`);
}

console.log(commit.sha);
