const fs = require('node:fs');
const path = require('node:path');
const ts = require('C:/Users/HieuKa/AppData/Local/New Hermes/hermes-agent/node_modules/typescript');
const root = 'C:/c/Users/HieuKa/Desktop/hermes-rewind-archive-20260810/apps/desktop/src/i18n';
const files = ['ar.ts', 'en.ts', 'ja.ts', 'zh-hant.ts', 'zh.ts'];
const results = [];
let failed = false;
for (const file of files) {
  const source = fs.readFileSync(path.join(root, file), 'utf8');
  const result = ts.transpileModule(source, {
    fileName: file,
    reportDiagnostics: true,
    compilerOptions: {
      target: ts.ScriptTarget.ES2022,
      module: ts.ModuleKind.ESNext,
    },
  });
  const errors = (result.diagnostics || [])
    .filter((d) => d.category === ts.DiagnosticCategory.Error)
    .map((d) => ts.flattenDiagnosticMessageText(d.messageText, '\n'));
  if (errors.length) failed = true;
  results.push({ file, syntaxErrors: errors });
}
console.log(JSON.stringify({ checked: files.length, failed, results }, null, 2));
process.exit(failed ? 1 : 0);
