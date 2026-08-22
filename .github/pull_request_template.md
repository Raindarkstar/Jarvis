## 变更摘要

<!-- 用几句话说明本 PR 做了什么，以及为什么需要它。 -->

## 验证方式

- [ ] `python -m unittest discover -s tests -v`
- [ ] `bash -n install.sh jarvis-client.sh rain-ai-client.sh`
- [ ] `jarvis doctor`（如涉及环境或依赖）

## 安全与兼容性

- [ ] 未提交 `.env`、个人记忆、聊天历史或录音文件
- [ ] 涉及系统命令/文件操作时，说明了权限边界和失败行为
- [ ] 如涉及 X11/Wayland，已说明测试环境
