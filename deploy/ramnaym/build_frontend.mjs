// Build public assets without reading dotenv files or exposing custom env values.
import {createRequire} from 'node:module';
import {dirname, resolve} from 'node:path';
import {fileURLToPath, pathToFileURL} from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const output = process.argv[2];
if (!output) throw new Error('An isolated output directory is required.');
const require = createRequire(resolve(root, 'frontend/package.json'));
const {build} = await import(pathToFileURL(require.resolve('vite')).href);
const {default: react} = await import(pathToFileURL(require.resolve('@vitejs/plugin-react')).href);
await build({
  root: resolve(root, 'frontend'), configFile: false, envDir: false, envPrefix: [],
  mode: 'production', plugins: [react()],
  build: {outDir: resolve(output), emptyOutDir: false},
});
