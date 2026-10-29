#!/usr/bin/env node
/**
 * 发版前复核：tag、提交、package.json 的 version 三者必须指向同一个版本。
 *
 * 为什么需要它
 *   版本号是 package.json 里唯一写死的东西，而更新检查是拿它跟 registry 上的
 *   版本列表比的。手工 `git tag v0.5.1` 却忘了改 version，后果不是「装不上」，
 *   而是**永远报有新版**：装完 v0.5.1，磁盘上读到的仍是 0.5.0，下一轮判决照样
 *   是 available，于是每轮都去装同一个版本、被 already-installed 拒掉、退避
 *   翻倍到上限，永远如此。这条路径在 dsh-pwsh-plugin 上实测过，不是猜测。
 *
 *   本插件的更新源是 npm registry（不是 GitHub 标签），所以除了版本号一致，
 *   market-index.json 里的版本指针也必须跟上，否则插件市场里看到的是旧版本。
 *
 * 怎么用
 *   npm version patch -m 'chore(release): %s'   # 改 version + 提交 + 打 annotated tag
 *   node scripts/release-check.mjs              # 复核三者对齐
 *   git push origin main <tag>
 *
 * 退出码 0 = 可以发；1 = 有问题，原因打在 stderr 上。
 */

import { execFileSync } from 'node:child_process'
import { readFileSync, existsSync } from 'node:fs'

const fail = (message) => {
  console.error('release-check: ' + message)
  process.exit(1)
}

/**
 * 跑一条 git 命令。失败时不抛出，而是把 git 自己的 stderr 一起带回来。
 *
 * 为什么要把 stderr 留下：在受限令牌的会话里 git 根本起不来（建不了匿名管道，报
 * "cannot create standard input pipe"），那时 stderr 为空、退出码非零。只返回 undefined
 * 的版本会把它说成「当前目录不是 git 仓库」—— 一句听起来像配置问题的假话，实测误导过一轮。
 */
const gitRaw = (...args) => {
  try {
    return { output: execFileSync('git', args, { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }).trim() }
  } catch (error) {
    const stderr = typeof error?.stderr === 'string' ? error.stderr.trim() : ''
    return { reason: stderr !== '' ? stderr : 'git exited without a message (' + (error?.code ?? 'unknown') + ')' }
  }
}

/** 大多数检查只关心「拿到了没有」，失败原因由需要解释的地方用 gitRaw 取。 */
const git = (...args) => gitRaw(...args).output

const pkg = JSON.parse(readFileSync(new URL('../package.json', import.meta.url), 'utf8'))
const version = pkg.version
if (typeof version !== 'string' || !/^\d+\.\d+\.\d+$/.test(version)) {
  fail('package.json 的 version 必须是三段版本号，当前是 ' + String(version))
}

const status = gitRaw('status', '--porcelain')
if (status.output === undefined) {
  fail('git status 跑不通：' + status.reason
    + '\n（报错里出现 EPERM、pipe 或 Access is denied，说明当前会话的受限令牌起不了 git 子进程，'
    + '换一个正常权限的终端来跑这个脚本，而不是去查仓库本身。）')
}
const dirty = status.output
if (dirty.length > 0) {
  fail('工作区不干净，先提交或 stash：\n' + dirty)
}

// pyproject.toml 的版本必须跟着 package.json 走：Python 侧的 `ui_locate` 包
// 就是同一次发布装进去的，两个号不一致时，venv 里读到的版本会和 JS 侧对不上。
const pyproject = readFileSync(new URL('../pyproject.toml', import.meta.url), 'utf8')
const declaredPython = pyproject.match(/^version\s*=\s*"([^"]+)"/m)?.[1]
if (declaredPython !== version) {
  fail('pyproject.toml 的 version 是 ' + String(declaredPython) + '，但 package.json 是 ' + version
    + '。Python 侧和 JS 侧必须同号，否则装完两边版本对不上。')
}

// 更新源是 npm registry，所以 market-index.json 的版本指针也要对齐；它决定的是
// 「插件市场里看到哪个版本」，与 npm 上的实际内容无关，因此两者必须一起改。
const indexPath = new URL('../market-index.json', import.meta.url)
if (existsSync(indexPath)) {
  const index = JSON.parse(readFileSync(indexPath, 'utf8'))
  const entry = index.plugins?.find((item) => item.name === pkg.name)
  if (entry === undefined) {
    fail('market-index.json 里没有 ' + pkg.name + ' 这个插件')
  }
  const published = entry.versions?.[0]?.version
  if (published !== version) {
    fail('market-index.json 指向 ' + String(published) + '，但 package.json 是 ' + version
      + '。重新生成：python scripts/make_index.py --npm --out market-index.json')
  }
} else {
  fail('market-index.json 不存在；插件市场的索引就是它')
}

// --exact-match 只在 HEAD 正好落在某个 tag 上时给结果；否则是非零退出码，git() 返回 undefined。
const exact = git('describe', '--tags', '--exact-match')
const expected = 'v' + version

if (exact === undefined) {
  // expected 这个 tag 已经用掉了，而 HEAD 又在它之后：说明 tag 之后还有未发版的提交。
  // 这时候重打同名 tag 会把已发布的版本移到新提交上，下游拿到的「v0.5.0」就换了内容。
  if (git('rev-parse', '--verify', '--quiet', 'refs/tags/' + expected) !== undefined) {
    fail('tag ' + expected + ' 已经指向别的提交，而 HEAD 在它之后还有改动。'
      + '先升版本：npm version patch -m "chore(release): %s"')
  }
  // 还没有这个 tag：这正是「该打 tag 了」的状态，把该打的名字说出来。
  console.log('release-check: HEAD 还没有 tag —— 该打的是 ' + expected + '（package.json 是 ' + version + '）')
  console.log('release-check: git tag -a ' + expected + ' -m "' + expected + '"')
  process.exit(0)
}

if (exact !== expected) {
  fail('HEAD 上的 tag 是 ' + exact + '，但 package.json 的 version 对应 ' + expected
    + '。两者必须一致，否则更新检查会永远报有新版。')
}

const tagVersion = git('show', exact + ':package.json')
if (tagVersion === undefined) {
  fail('读不出 ' + exact + ' 那个提交里的 package.json')
}
const declared = JSON.parse(tagVersion).version
if (declared !== version) {
  fail(exact + ' 指向的提交里 version 是 ' + declared + '，与当前 ' + version + ' 不一致')
}

console.log('release-check: OK —— ' + exact + ' / 提交 ' + (git('rev-parse', '--short', 'HEAD') ?? '?')
  + ' / version ' + version + ' 三者对齐')
