#!/usr/bin/env tsx
/**
 * 对**任意内容目录**跑一遍 App 的内容门禁逻辑（只读校验工具）。
 *
 * 为什么需要它
 * ------------
 * `tools/validate-content.ts`（`npm run gate`）把内容目录写死成 `<项目根>/content`。
 * 而爬虫需要在**副本**上做端到端验证（采集 → 候选 → 写回 → 门禁），
 * 不能拿真实内容库做实验。
 *
 * 所以这里**复用 App 自己的校验实现**（`loadContent` + `validateContent` + `summarize`），
 * 只是把目录变成参数。校验规则一行都没有重写——重写就不是"同一道门禁"了，
 * 那样验证出来的结论也不能迁移到真实内容上。
 *
 * 它刻意放在爬虫目录下，**不改动 App 的任何工具**。
 *
 * 用法：
 *     npx tsx 爬虫/tools/check_content_dir.ts <内容目录>
 */

import { resolve } from 'node:path';
import { loadContent } from '../../app/content/load';
import { summarize, validateContent } from '../../src/shared/validate';

function main(): number {
  const target = process.argv[2];
  if (!target) {
    console.error('用法：npx tsx 爬虫/tools/check_content_dir.ts <内容目录>');
    return 2;
  }
  const dir = resolve(target);

  const { concepts, sources, parseErrors } = loadContent(dir);

  if (parseErrors.length > 0) {
    console.error(`\n✗ YAML 解析失败（${parseErrors.length} 个文件）`);
    for (const e of parseErrors) console.error(`  ${e.file}: ${e.message}`);
    return 1;
  }
  if (concepts.length === 0) {
    console.error(`\n✗ ${dir} 下没有找到任何概念`);
    return 1;
  }

  const issues = validateContent(concepts);
  const { errors, warnings } = summarize(issues);

  console.log(`\n内容门禁（复用 App 的校验实现）：${dir}`);
  console.log(`  ${concepts.length} 个概念，来自 ${sources.length} 个源文件\n`);

  for (const e of errors) console.error(`  error  ${e.id ?? '(全库)'}：${e.message}`);
  for (const w of warnings) console.log(`  warn   ${w.id ?? '(全库)'}：${w.message}`);

  if (errors.length > 0) {
    console.error(`\n✗ 门禁未通过：${errors.length} error / ${warnings.length} warning\n`);
    return 1;
  }
  console.log(`\n✓ 门禁通过（0 error，${warnings.length} warning）\n`);
  return 0;
}

process.exit(main());
