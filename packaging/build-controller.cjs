// 构建期将 MIT 上游源码封装为 Node SEA；安装包不携带源码或外部 Node 依赖。
const fs = require('node:fs');
const path = require('node:path');
const cp = require('node:child_process');
const root = path.resolve(__dirname, '..');
const esbuild = require(path.join(root, '.build-tools/node_modules/esbuild'));
const output = path.join(root, 'build/controller');
fs.mkdirSync(output, { recursive: true });
const entry = path.join(output, 'controller.cjs');
const version = fs.readFileSync(path.join(root, 'VERSION'), 'utf8').trim();
esbuild.buildSync({
  entryPoints: [path.join(root, 'third_party/route-controller/controller.mjs')],
  bundle: true, platform: 'node', target: 'node22', format: 'cjs', outfile: entry,
  banner: { js: `if (process.argv.includes('--version')) { console.log('openai-proxy-kit controller ${version} (SEA=' + require('node:sea').isSea() + ')'); process.exit(0); }` },
});
const config = path.join(output, 'sea.json');
const blob = path.join(output, 'sea.blob');
fs.writeFileSync(config, JSON.stringify({ main: entry, output: blob, disableExperimentalSEAWarning: true,
                                        useSnapshot: false, useCodeCache: false }));
cp.execFileSync(process.execPath, ['--experimental-sea-config', config], { stdio: 'inherit' });
const executable = path.join(output, 'openai-route-controller');
fs.copyFileSync(process.execPath, executable);
fs.chmodSync(executable, 0o755);
// Strip before SEA injection; stripping the final executable can destroy the blob.
cp.execFileSync('strip', ['--strip-all', executable]);
cp.execFileSync(process.execPath, [path.join(root, '.build-tools/node_modules/postject/dist/cli.js'),
  executable, 'NODE_SEA_BLOB', blob, '--sentinel-fuse', 'NODE_SEA_FUSE_fce680ab2cc467b6e072b8b5df1996b2'],
  { stdio: 'inherit' });
cp.execFileSync(executable, ['--version'], { stdio: 'inherit', env: { PATH: '/nonexistent' } });
