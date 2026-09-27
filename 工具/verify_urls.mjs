// 验证插件真正使用的三条下载地址是否可用（只取开头若干字节，不下整包）。
// 本机 github.com:443 被挡，所以另外用 api.github.com 做一份等价证据。

const OWNER = "creeperboo";
const REPO = "anki-exam-vocab-stats";
const token = process.env.GH_TOKEN;

const targets = [
  ["raw version.txt", `https://raw.githubusercontent.com/${OWNER}/${REPO}/main/version.txt`],
  [
    "releases/latest/download/exam_vocab_stats.ankiaddon",
    `https://github.com/${OWNER}/${REPO}/releases/latest/download/exam_vocab_stats.ankiaddon`,
  ],
  [
    "releases/latest/download/exam_materials_audio.zip",
    `https://github.com/${OWNER}/${REPO}/releases/latest/download/exam_materials_audio.zip`,
  ],
];

for (const [label, url] of targets) {
  try {
    const res = await fetch(url, {
      headers: { Range: "bytes=0-63", "User-Agent": "exam-vocab-stats-verify" },
      redirect: "follow",
    });
    const buf = Buffer.from(await res.arrayBuffer());
    console.log(
      `${label}: HTTP ${res.status}  取到 ${buf.length} 字节  头 8 字节 = ${buf
        .subarray(0, 8)
        .toString("hex")}`,
    );
  } catch (err) {
    console.log(`${label}: 连不上 —— ${err.cause?.message || err.message}`);
  }
}

const API = `https://api.github.com/repos/${OWNER}/${REPO}`;
const HEAD = {
  Authorization: `Bearer ${token}`,
  Accept: "application/vnd.github+json",
  "X-GitHub-Api-Version": "2022-11-28",
  "User-Agent": "exam-vocab-stats-verify",
};

const latest = await fetch(`${API}/releases/latest`, { headers: HEAD });
const latestJson = await latest.json();
console.log(
  `\nAPI releases/latest: HTTP ${latest.status}  tag=${latestJson.tag_name}  draft=${latestJson.draft}  prerelease=${latestJson.prerelease}`,
);
console.log(`附件数 ${latestJson.assets?.length}`);

for (const asset of latestJson.assets ?? []) {
  const res = await fetch(asset.url, {
    headers: { ...HEAD, Accept: "application/octet-stream", Range: "bytes=0-63" },
    redirect: "follow",
  });
  const buf = Buffer.from(await res.arrayBuffer());
  console.log(
    `  ${asset.name}: HTTP ${res.status}  ${buf.length} 字节  声明大小 ${asset.size}  sha256 ${asset.digest}`,
  );
}
