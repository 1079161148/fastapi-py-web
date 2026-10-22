import DOMPurify from 'dompurify'
import MarkdownIt from 'markdown-it'

/**
 * 聊天回复的 Markdown 渲染。
 *
 * 回复内容来自模型，属于不可信输入：markdown-it 关掉 html 解析，
 * 产出后再过一遍 DOMPurify，拦掉 <script>、onerror、javascript: 链接等注入。
 *
 * breaks: true —— 模型输出常用单换行分行，此前靠 CSS white-space: pre-wrap
 * 保留换行，改由渲染器负责，避免换行语义丢失。
 */
const md = new MarkdownIt({
  html: false,
  linkify: true,
  breaks: true,
})

export function renderMarkdown(source: string): string {
  return DOMPurify.sanitize(md.render(source))
}

/**
 * 摘掉成对标记里没配上的那一个。
 *
 * 逐字吐出时 `**加粗` 这种半截语法会被 markdown-it 当普通字符渲染，
 * 屏幕上就是一闪一闪的星号。出现次数为奇数说明最后一个没闭合，先摘掉它；
 * 闭合后会立刻补回来并正常加粗。反引号不处理：围栏代码块是三个，
 * 误摘会让代码块短暂错乱，代价比看到反引号大。
 */
function stripUnclosedStrong(source: string): string {
  let text = source
  for (const marker of ['**', '__']) {
    if (text.split(marker).length % 2 === 0) {
      const index = text.lastIndexOf(marker)
      text = text.slice(0, index) + text.slice(index + marker.length)
    }
  }
  return text
}

/**
 * 流式中间态渲染：摘掉未闭合的强调标记与末尾孤立的符号。
 * 只用于流式中，最终文本仍走 renderMarkdown。
 */
export function renderStreamingMarkdown(source: string): string {
  return renderMarkdown(stripUnclosedStrong(source).replace(/[*_~]{1,3}$/, ''))
}
