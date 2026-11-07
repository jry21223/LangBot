// Synthetic browser fixture server. Dependency image remains read-only.
import { createServer } from 'vite';
const server = await createServer({
  cacheDir: '/tmp/native-vite-cache',
  server: { host: '127.0.0.1', port: 4173, strictPort: true },
});
console.info('Isolated native UI fixture', process.version, server.config.cacheDir);
await server.listen();
