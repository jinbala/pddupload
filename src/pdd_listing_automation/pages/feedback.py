"""仅采信短、可见、完整的反馈；不把整页说明文字当成提交结果。"""
from __future__ import annotations

import re

from playwright.async_api import Frame, FrameLocator, Page

SUCCESS = re.compile(r"^(?:提交成功|任务创建成功|上传成功|上货成功|上架成功|发布成功)[！!。\s]*$")
REJECTION = re.compile(
    r"^(?:提交失败|上传失败|上货失败|发布失败|未提交成功|授权已过期|授权过期|"
    r"已达上限|达到上限|操作频繁|无权限)(?:$|[！!。，,：:\s])"
)
FEEDBACK_JS = r"""() => {
  const success = /^(提交成功|任务创建成功|上传成功|上货成功|上架成功|发布成功)[！!。\s]*$/;
  const failure = /^(提交失败|上传失败|上货失败|发布失败|未提交成功|授权已过期|授权过期|已达上限|达到上限|操作频繁|无权限)($|[！!。，,：:\s])/;
  const nodes = document.querySelectorAll(
    '[role="alert"], [role="status"], .ant-message-notice-content, .ant-notification-notice-message, '
    + '.ant-modal-confirm-content, div, p, span');
  const result = [];
  for (const el of nodes) {
    // 只读短反馈节点，避免每次轮询把整张规格表传回 Python。
    if (el.childElementCount > 3) continue;
    const text = (el.textContent || '').trim();
    if (text.length > 200 || !(success.test(text) || failure.test(text))) continue;
    if (!el.getClientRects().length || getComputedStyle(el).visibility === 'hidden') continue;
    result.push(text);
  }
  return [...new Set(result)];
}"""


async def visible_feedback(root: Page | FrameLocator | Frame) -> list[str]:
    # document 在 iframe 内执行，不受长表单文本截断影响。
    return await root.locator("body").evaluate(FEEDBACK_JS, timeout=1_000)
