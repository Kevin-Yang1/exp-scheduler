import { Moon, Sun } from 'lucide-react';
import { useEffect, useState } from 'react';

type Theme = 'light' | 'dark';
const STORAGE_KEY = 'exp-scheduler-theme';

function preferredTheme(): Theme {
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (saved === 'light' || saved === 'dark') return saved;
  } catch { /* Keep switching available when browser storage is unavailable. */ }
  return matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

export default function ThemeToggle() {
  const [theme, setTheme] = useState<Theme>(() => document.documentElement.dataset.theme === 'dark' ? 'dark' : 'light');
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
  }, [theme]);

  useEffect(() => {
    const media = matchMedia('(prefers-color-scheme: dark)');
    const update = () => setTheme(preferredTheme());
    const storage = (event: StorageEvent) => {
      if (event.key === STORAGE_KEY || event.key === null) update();
    };
    media.addEventListener('change', update);
    window.addEventListener('storage', storage);
    return () => {
      media.removeEventListener('change', update);
      window.removeEventListener('storage', storage);
    };
  }, []);

  const label = theme === 'light' ? '切换为暗色外观' : '切换为亮色外观';
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      className="flex items-center justify-center w-8 h-8 shrink-0 rounded-lg border border-slate-200 bg-slate-50 text-slate-600 hover:bg-slate-100 transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500"
      onClick={() => {
        const next = theme === 'light' ? 'dark' : 'light';
        setTheme(next);
        try { localStorage.setItem(STORAGE_KEY, next); } catch { /* Use the selection for this session. */ }
      }}
    >
      {theme === 'light' ? <Moon size={15} aria-hidden="true" /> : <Sun size={15} aria-hidden="true" />}
    </button>
  );
}
