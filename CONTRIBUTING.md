# tripo2api 仓库约定

## Git

- **commit message 不要添加 `Co-Authored-By: Claude ...` trailer**。
  已在首个 commit（`5673c9d`）保留历史标注，**后续提交一律不再写**，
  以免 Contributors 列表重复出现 AI 账号。
- commit message 首行用中文祈使句概括改动，控制在 50 字内。
- 正文分点说明动机与关键实现，末尾如有需要可加 `Co-Authored-By` 指向**人类**协作者。

## 提交前检查

```bash
git add -A
git ls-files | grep -iE "config\.json$|accounts\.json|\.db$" && echo "❌ 凭证被暂存" || echo "✅"
```

凭证类文件（`config.json` / `accounts.json` / `data/`）必须始终被 `.gitignore` 挡住。

## 代码风格

- 纯标准库优先；新增第三方依赖需在 README 的快速开始里说明安装命令。
- 服务端注释解释**为什么**（踩过的坑、非直觉行为），不复述代码在做什么。
- 中文注释与文档，代码标识符用英文。
- 控制台改动后用 `node --check` 验证 JS 语法：
  ```bash
  python3 -c "s=open('console.html').read(); i=s.find('<script>'); j=s.rfind('</script>'); open('/tmp/c.js','w').write(s[i+8:j])"
  node --check /tmp/c.js
  ```

## 协议改动

涉及 Tripo 端点/字段的行为变更，要同步更新：
- `README.md`（模型清单、API 表、实现说明）
- `docs/RECON.md`（证据链与实测记录）
