/**
 * ui-locate —— 浏览器半身。
 *
 * 只做一件事：在设置里放一页「运行环境」，让用户能看见 Python 侧装到哪一步、
 * 装在哪、还缺什么，并且能一键补齐。
 *
 * ## 为什么这件事必须在界面上做
 *
 * Python 的 venv 与 77MB 的检测模型都不在 npm 包里。以前要靠用户自己在终端
 * 跑 `scripts/bootstrap.ps1`，但那条路对普通用户是断的：机器上未必有 uv，
 * 有 uv 也可能被权限拦住，Windows 上直接敲 `python` 还会跳到应用商店。
 *
 * 现在 Host 半身会用 DSH 自带的 Python 自己把环境搭好。界面负责的是「让它
 * 可见、可重试、可搬家」——装到什么程度、装在哪里、换台机器后模型要不要重下。
 *
 * ## 数据从哪来
 *
 * 走本插件自己的同源 `/api/ui-locate/*` 路由，不接 Remote 绑定：那几个路由
 * 由同一个 Host 进程提供，跨内核版本都在。四条：`/status`、`/defaults`、
 * `/setup`、`/progress`。
 *
 * 手写 ModuleLoader 包，不需要构建步骤，除 shell 已提供的 `react` 之外没有依赖。
 * 所有颜色取自主题变量，换配色方案时这页跟着变。
 */
window.__ModuleLoader__.load({
  // 必须是包名，不是插件的短名。client-modules 的 `arrive()` 拿着包名去查
  // 已经注册的 factory（`if (this.factories.has(id))`），名字对不上就一路
  // 抛 `loaded without registering "ui-locate-mcp"`，而那条错误只进浏览器
  // 控制台——宿主那头毫无异样，设置页里就是干脆没有这一栏。
  id: 'ui-locate-mcp',
  factory: require => {
    const module = { exports: {} }
    const exports = module.exports
    const React = require('react')
    const { createElement: h, Fragment, useState, useEffect, useRef, useCallback } = React

    const NS = 'settings.uiLocate'
    /** 路由前缀；与 Host 半身注册的路径必须一致。 */
    const API = '/api/ui-locate'
    const inject = ['slots', 'locale']

    // ── 文案 ──────────────────────────────────────────────────────────────────
    const DICT = {
      zh: {
        nav: 'ui-locate',
        title: 'ui-locate 运行环境',
        sub: '本插件用一个本地检测模型把屏幕上的元素找出来。Python 侧与模型不在插件包里，由 DSH 自带的运行时按需安装。',
        'state.ready': '就绪',
        'state.missing': '未安装',
        'state.partial': '不完整',
        'state.running': '准备中',
        'state.failed': '失败',
        'sec.locations': '存放位置',
        'sec.locations.hint': '留空使用默认值。模型单独放是因为它可能很大，也可能早就有一份了。',
        'field.home': '运行目录',
        'field.model': '模型目录',
        'field.backend': '推理后端',
        'backend.directml': 'DirectML（推荐，NVIDIA / AMD / Intel 通用）',
        'backend.cuda': 'CUDA（仅 NVIDIA，快一点但多 1.7GB）',
        'backend.cpu': '纯 CPU（最慢，没显卡时用）',
        'btn.prepare': '准备环境',
        'btn.repair': '重新安装',
        'btn.save': '保存位置',
        'hint.first': '首次准备要下载约 130MB 的依赖与 77MB 的模型，视网速需要几分钟。',
        'hint.ready': '环境已就绪，工具可以直接使用。',
        'hint.job': '装好之后需要重启 DSH，MCP 客户端才会连上新解释器。',
        'status.venv': 'Python 环境',
        'status.model': '检测模型',
        'status.python': 'Python 版本',
        'status.pkg': '插件版本',
        'status.backend': '可用后端',
        'step.of': '当前步骤',
        'err.title': '准备失败',
        'err.retry': '可以再试一次；如果反复失败，看下面这段输出。',
        yes: '已就绪',
        no: '缺失',
        unknown: '未知',
      },
      en: {
        nav: 'ui-locate',
        title: 'ui-locate runtime',
        sub: 'This plugin uses a local detection model to find on-screen elements. The Python side and the model are not in the npm package; DSH installs them on demand from its own runtime.',
        'state.ready': 'Ready',
        'state.missing': 'Not installed',
        'state.partial': 'Incomplete',
        'state.running': 'Preparing',
        'state.failed': 'Failed',
        'sec.locations': 'Locations',
        'sec.locations.hint': 'Leave blank for the default. The model lives apart because it can be large, and you may already have one.',
        'field.home': 'Runtime directory',
        'field.model': 'Model directory',
        'field.backend': 'Inference backend',
        'backend.directml': 'DirectML (recommended; NVIDIA, AMD and Intel)',
        'backend.cuda': 'CUDA (NVIDIA only; slightly faster, 1.7GB heavier)',
        'backend.cpu': 'CPU only (slowest; for machines without a GPU)',
        'btn.prepare': 'Prepare runtime',
        'btn.repair': 'Reinstall',
        'btn.save': 'Save locations',
        'hint.first': 'The first run downloads about 130MB of dependencies and a 77MB model; give it a few minutes.',
        'hint.ready': 'The runtime is ready; the tools work as they are.',
        'hint.job': 'After it finishes, restart DSH so the MCP client picks up the new interpreter.',
        'status.venv': 'Python environment',
        'status.model': 'Detection model',
        'status.python': 'Python version',
        'status.pkg': 'Plugin version',
        'status.backend': 'Available backends',
        'step.of': 'Current step',
        'err.title': 'Preparation failed',
        'err.retry': 'Trying again is safe. If it keeps failing, the output below says where.',
        yes: 'Present',
        no: 'Missing',
        unknown: 'Unknown',
      },
    }

    // ── 样式 ──────────────────────────────────────────────────────────────────
    // 全部走主题变量：深浅配色、强调色切换时这一页不用改。
    const CSS = `
.ul_root{display:flex;flex-direction:column;gap:16px;padding:2px 0 8px}
.ul_hero{position:relative;display:flex;flex-direction:column;gap:10px;padding:20px 22px;border-radius:18px;
  border:1px solid color-mix(in srgb,var(--dsw-alias-border-l2) 78%,transparent);
  background:linear-gradient(165deg,color-mix(in srgb,var(--dsw-alias-bg-layer-3) 88%,transparent),color-mix(in srgb,var(--dsw-alias-bg-layer-1) 82%,transparent));
  overflow:hidden}
.ul_hero::before{content:"";position:absolute;inset:0;pointer-events:none;
  background:radial-gradient(120% 150% at 12% -20%,color-mix(in srgb,var(--dsw-alias-state-business-primary) 12%,transparent),transparent 58%)}
.ul_hero>*{position:relative}
.ul_head{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.ul_title{margin:0;font-size:20px;font-weight:740;letter-spacing:-.4px;color:var(--dsw-alias-label-primary)}
.ul_sub{margin:0;font-size:12.5px;line-height:1.6;color:var(--dsw-alias-label-secondary);max-width:76ch}
.ul_pill{margin-left:auto;font-size:11px;font-weight:650;padding:3px 10px;border-radius:999px;
  border:1px solid var(--dsw-alias-border-l1);color:var(--dsw-alias-label-secondary);
  background:color-mix(in srgb,var(--dsw-alias-bg-layer-1) 60%,transparent);white-space:nowrap}
.ul_pill.ok{color:var(--dsw-alias-state-success-primary);border-color:color-mix(in srgb,var(--dsw-alias-state-success-primary) 45%,transparent);
  background:color-mix(in srgb,var(--dsw-alias-state-success-primary) 10%,transparent)}
.ul_pill.warn{color:var(--dsw-alias-state-warning-primary,#f0a441);border-color:color-mix(in srgb,var(--dsw-alias-state-warning-primary,#f0a441) 45%,transparent);
  background:color-mix(in srgb,var(--dsw-alias-state-warning-primary,#f0a441) 10%,transparent)}
.ul_pill.bad{color:var(--dsw-alias-state-error-primary);border-color:color-mix(in srgb,var(--dsw-alias-state-error-primary) 45%,transparent);
  background:color-mix(in srgb,var(--dsw-alias-state-error-primary) 10%,transparent)}
.ul_card{display:flex;flex-direction:column;gap:12px;padding:16px 18px;border-radius:14px;
  border:1px solid var(--dsw-alias-border-l1);background:color-mix(in srgb,var(--dsw-alias-bg-layer-1) 55%,transparent)}
.ul_cardhd{display:flex;align-items:baseline;gap:8px;flex-wrap:wrap}
.ul_cardname{font-size:13.5px;font-weight:660;color:var(--dsw-alias-label-primary)}
.ul_cardhint{font-size:11.5px;color:var(--dsw-alias-label-tertiary)}
.ul_grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:12px}
.ul_field{display:flex;flex-direction:column;gap:5px;min-width:0}
.ul_label{font-size:11.5px;font-weight:620;color:var(--dsw-alias-label-secondary)}
.ul_input{height:32px;padding:0 10px;border-radius:8px;font-size:12px;
  border:1px solid var(--dsw-alias-border-l1);background:var(--dsw-alias-bg-layer-1);
  color:var(--dsw-alias-label-primary);font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
  transition:border-color .16s var(--ease-settle,ease),box-shadow .16s var(--ease-settle,ease)}
.ul_input:focus-visible{outline:none;border-color:color-mix(in srgb,var(--dsw-alias-state-business-primary) 70%,transparent);
  box-shadow:0 0 0 3px color-mix(in srgb,var(--dsw-alias-state-business-primary) 14%,transparent)}
.ul_select{height:32px;padding:0 8px;border-radius:8px;font-size:12px;
  border:1px solid var(--dsw-alias-border-l1);background:var(--dsw-alias-bg-layer-1);color:var(--dsw-alias-label-primary)}
.ul_facts{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px 18px}
.ul_fact{display:flex;flex-direction:column;gap:3px;min-width:0}
.ul_factk{font-size:11px;color:var(--dsw-alias-label-tertiary)}
.ul_factv{font-size:12.5px;color:var(--dsw-alias-label-primary);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ul_factv.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11.5px}
.ul_factv.ok{color:var(--dsw-alias-state-success-primary)}
.ul_factv.bad{color:var(--dsw-alias-state-error-primary)}
.ul_row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.ul_btn{height:30px;padding:0 14px;border-radius:8px;font-size:12px;font-weight:620;cursor:pointer;
  border:1px solid var(--dsw-alias-border-l1);background:color-mix(in srgb,var(--dsw-alias-bg-layer-2) 80%,transparent);
  color:var(--dsw-alias-label-primary);transition:background .16s var(--ease-settle,ease),border-color .16s var(--ease-settle,ease)}
.ul_btn:hover:not(:disabled){background:var(--dsw-alias-bg-layer-3);border-color:var(--dsw-alias-border-l2)}
.ul_btn.primary{border-color:transparent;color:#fff;background:var(--dsw-alias-state-business-primary)}
.ul_btn.primary:hover:not(:disabled){background:color-mix(in srgb,var(--dsw-alias-state-business-primary) 86%,#fff)}
.ul_btn:disabled{opacity:.5;cursor:not-allowed}
.ul_bar{position:relative;height:6px;border-radius:999px;overflow:hidden;background:var(--dsw-alias-bg-layer-2);margin-top:2px}
.ul_barfill{position:absolute;inset:0 auto 0 0;border-radius:999px;background:var(--dsw-alias-state-business-primary);
  animation:ul_slide 1.6s var(--ease-instrument,cubic-bezier(.4,0,.2,1)) infinite}
@keyframes ul_slide{0%{left:0;right:100%}50%{left:0;right:0}100%{left:100%;right:0}}
.ul_step{font-size:11.5px;color:var(--dsw-alias-label-secondary)}
.ul_step b{font-weight:660;color:var(--dsw-alias-label-primary)}
.ul_note{font-size:11.5px;line-height:1.6;color:var(--dsw-alias-label-tertiary)}
.ul_err{padding:10px 12px;border-radius:10px;font-size:11.5px;line-height:1.55;word-break:break-word;
  border:1px solid color-mix(in srgb,var(--dsw-alias-state-error-primary) 40%,transparent);
  background:color-mix(in srgb,var(--dsw-alias-state-error-primary) 8%,transparent);color:var(--dsw-alias-state-error-primary)}
.ul_err pre{margin:6px 0 0;font-size:11px;white-space:pre-wrap;word-break:break-all;
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace;opacity:.9}
`

    /**
     * 一个带超时的 JSON 请求。
     *
     * 设置页可能在 Host 正忙时打开，没有超时的话一次挂起的 fetch 会让整页
     * 永远停在加载态。长任务不走这里——`/setup` 立刻返回，结果靠轮询。
     */
    async function api(path, options) {
      const { timeout = 15000, ...rest } = options ?? {}
      const controller = new AbortController()
      const timer = setTimeout(() => controller.abort(), timeout)
      try {
        const response = await fetch(`${API}${path}`, { ...rest, redirect: 'error', signal: controller.signal })
        const text = await response.text()
        let payload
        try {
          payload = text === '' ? {} : JSON.parse(text)
        } catch {
          payload = { error: text.slice(0, 300) }
        }
        if (!response.ok) throw new Error(payload?.error ?? `HTTP ${response.status}`)
        return payload
      } finally {
        clearTimeout(timer)
      }
    }

    /** 把字节数说成人话。 */
    function human(bytes) {
      if (typeof bytes !== 'number' || !Number.isFinite(bytes) || bytes <= 0) return ''
      const units = ['B', 'KB', 'MB', 'GB']
      let value = bytes
      let index = 0
      while (value >= 1024 && index < units.length - 1) {
        value /= 1024
        index += 1
      }
      return `${value.toFixed(value >= 100 || index === 0 ? 0 : 1)} ${units[index]}`
    }

    function SettingsPage(props) {
      const t = props.t
      const [status, setStatus] = useState(undefined)
      const [job, setJob] = useState(undefined)
      const [error, setError] = useState('')
      const [home, setHome] = useState('')
      const [modelDir, setModelDir] = useState('')
      const [backend, setBackend] = useState('directml')
      const [loading, setLoading] = useState(true)
      // 用户改过输入框之后就别再被轮询结果覆盖回去：那会让人打字打到一半跳字。
      const edited = useRef(false)

      const apply = useCallback(payload => {
        setStatus(payload)
        if (!edited.current) {
          setHome(payload?.home ?? '')
          setModelDir(payload?.modelDir ?? '')
        }
        if (typeof payload?.backend === 'string') setBackend(payload.backend)
        if (payload?.job !== undefined) setJob(payload.job)
      }, [])

      useEffect(() => {
        let alive = true
        api('/status')
          .then(payload => {
            if (alive) apply(payload)
          })
          .catch(err => {
            if (alive) setError(String(err?.message ?? err))
          })
          .finally(() => {
            if (alive) setLoading(false)
          })
        return () => {
          alive = false
        }
      }, [apply])

      // 任务在跑时每秒问一次进度。装依赖可能要几分钟，长轮询不如短查询稳。
      const running = job?.state === 'running'
      useEffect(() => {
        if (!running) return undefined
        let alive = true
        const timer = setInterval(() => {
          api('/progress')
            .then(next => {
              if (!alive) return
              setJob(next)
              if (next?.state !== 'running') {
                // 刚跑完，把最新事实拉一遍，标签页才会从「准备中」跳到「就绪」。
                api('/status').then(payload => {
                  if (alive) apply(payload)
                }).catch(() => {})
              }
            })
            .catch(() => {})
        }, 1000)
        return () => {
          alive = false
          clearInterval(timer)
        }
      }, [running, apply])

      const start = useCallback(async force => {
        setError('')
        try {
          const payload = await api('/setup', {
            method: 'POST',
            headers: { 'content-type': 'application/json' },
            body: JSON.stringify({ home, modelDir, backend, force }),
          })
          setJob(payload.job)
        } catch (err) {
          setError(String(err?.message ?? err))
        }
      }, [home, modelDir, backend])

      const venv = status?.venv === true
      const model = status?.model === true
      const ready = venv && model
      const stateLabel = running
        ? t('state.running')
        : ready
          ? t('state.ready')
          : venv || model
            ? t('state.partial')
            : loading
              ? t('unknown')
              : t('state.missing')
      const pillClass = running ? 'warn' : ready ? 'ok' : venv || model ? 'warn' : 'bad'

      const fact = (label, value, tone) =>
        h('div', { className: 'ul_fact' }, [
          h('div', { className: 'ul_factk', key: 'k' }, label),
          h('div', { className: `ul_factv${tone ? ` ${tone}` : ''}`, key: 'v', title: typeof value === 'string' ? value : undefined }, value),
        ])

      return h('div', { className: 'ul_root' }, [
        h('div', { className: 'ul_hero', key: 'hero' }, [
          h('div', { className: 'ul_head', key: 'head' }, [
            h('h2', { className: 'ul_title', key: 't' }, t('title')),
            h('span', { className: `ul_pill ${pillClass}`, key: 'p' }, stateLabel),
          ]),
          h('p', { className: 'ul_sub', key: 'sub' }, t('sub')),
        ]),

        h('div', { className: 'ul_card', key: 'where' }, [
          h('div', { className: 'ul_cardhd', key: 'hd' }, [
            h('div', { className: 'ul_cardname', key: 'n' }, t('sec.locations')),
            h('div', { className: 'ul_cardhint', key: 'h' }, t('sec.locations.hint')),
          ]),
          h('div', { className: 'ul_grid', key: 'g' }, [
            h('label', { className: 'ul_field', key: 'home' }, [
              h('span', { className: 'ul_label', key: 'l' }, t('field.home')),
              h('input', {
                className: 'ul_input',
                key: 'i',
                value: home,
                spellCheck: false,
                placeholder: status?.defaults?.home ?? '',
                onChange: event => {
                  edited.current = true
                  setHome(event.target.value)
                },
              }),
            ]),
            h('label', { className: 'ul_field', key: 'model' }, [
              h('span', { className: 'ul_label', key: 'l' }, t('field.model')),
              h('input', {
                className: 'ul_input',
                key: 'i',
                value: modelDir,
                spellCheck: false,
                placeholder: status?.defaults?.modelDir ?? '',
                onChange: event => {
                  edited.current = true
                  setModelDir(event.target.value)
                },
              }),
            ]),
            h('label', { className: 'ul_field', key: 'backend' }, [
              h('span', { className: 'ul_label', key: 'l' }, t('field.backend')),
              h('select', {
                className: 'ul_select',
                key: 's',
                value: backend,
                disabled: running,
                onChange: event => setBackend(event.target.value),
              }, ['directml', 'cuda', 'cpu'].map(value =>
                h('option', { value, key: value }, t(`backend.${value}`)))),
            ]),
          ]),
        ]),

        h('div', { className: 'ul_card', key: 'facts' }, [
          h('div', { className: 'ul_cardhd', key: 'hd' }, [
            h('div', { className: 'ul_cardname', key: 'n' }, t('nav')),
          ]),
          h('div', { className: 'ul_facts', key: 'f' }, [
            fact(t('status.venv'), status ? (venv ? t('yes') : t('no')) : t('unknown'), status ? (venv ? 'ok' : 'bad') : ''),
            fact(t('status.model'), status ? (model ? (human(status.modelBytes) || t('yes')) : t('no')) : t('unknown'), status ? (model ? 'ok' : 'bad') : ''),
            fact(t('status.python'), status?.pythonVersion ?? t('unknown'), 'mono'),
            fact(t('status.pkg'), status?.installedVersion ?? t('unknown'), 'mono'),
            fact(t('status.backend'), status?.backend ?? t('unknown'), 'mono'),
          ]),
          h('div', { className: 'ul_row', key: 'row' }, [
            h('button', {
              className: 'ul_btn primary',
              key: 'go',
              disabled: running,
              onClick: () => void start(false),
            }, t('btn.prepare')),
            ready
              ? h('button', {
                  className: 'ul_btn',
                  key: 'again',
                  disabled: running,
                  onClick: () => void start(true),
                }, t('btn.repair'))
              : null,
          ]),
          running
            ? h(Fragment, { key: 'run' }, [
                h('div', { className: 'ul_step', key: 's' }, [
                  t('step.of'),
                  ' ',
                  h('b', { key: 'b' }, `${job?.step ?? ''}${job?.detail ? ` · ${job.detail}` : ''}`),
                ]),
                h('div', { className: 'ul_bar', key: 'bar' }, h('div', { className: 'ul_barfill' })),
              ])
            : null,
          !running && job?.state === 'failed'
            ? h('div', { className: 'ul_err', key: 'e' }, [
                h('div', { key: 't' }, t('err.title')),
                h('div', { key: 'r' }, t('err.retry')),
                h('pre', { key: 'p' }, String(job.error ?? '')),
              ])
            : null,
          error !== ''
            ? h('div', { className: 'ul_err', key: 'err' }, h('pre', {}, error))
            : null,
          h('div', { className: 'ul_note', key: 'note' }, running ? t('hint.job') : ready ? t('hint.ready') : t('hint.first')),
        ]),
      ])
    }

    // ── 接进 shell ────────────────────────────────────────────────────────────
    function apply(ctx) {
      const t = Object.assign(x => {
        const dict = DICT[activeLocale()] ?? DICT.zh
        return dict[x] ?? DICT.en[x] ?? x
      }, { locale: activeLocale() })

      ctx.effect(() => {
        const style = document.createElement('style')
        style.setAttribute('data-plugin', 'ui-locate-mcp')
        style.textContent = CSS
        document.head.appendChild(style)
        return () => style.remove()
      }, 'ui-locate: styles')

      ctx.slots.inject('settings.section', () => ctx.slots.register({
        name: 'settings.section',
        id: 'ui-locate',
        order: 42,
        label: () => (DICT[activeLocale()] ?? DICT.zh).nav,
        locale: NS,
      }, props => h(SettingsPage, { ...props, t })))
    }

    /** 当前界面语言。拿不到就按中文，本插件的读者大多在那一边。 */
    function activeLocale() {
      try {
        const lang = document.documentElement.lang
        return typeof lang === 'string' && lang.toLowerCase().startsWith('en') ? 'en' : 'zh'
      } catch {
        return 'zh'
      }
    }

    exports.apply = apply
    exports.inject = inject
    exports.name = 'ui-locate-mcp'
    exports.__test = { human, DICT }
    return module.exports
  },
})
