# exp-scheduler frontend

React/Vite source for the GPU Flow scheduler UI.

## Development

```bash
npm install
npm run dev
```

The dev server proxies API requests only when opened behind the scheduler backend. For production assets, build and copy `dist` into `src/exp_scheduler_app/static`:

```bash
npm run build
rm -rf ../src/exp_scheduler_app/static/assets
cp dist/index.html ../src/exp_scheduler_app/static/index.html
cp -R dist/assets ../src/exp_scheduler_app/static/assets
```
