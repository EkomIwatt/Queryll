import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import { App } from './App';
import { initTheme } from './ui/ThemeToggle';

import './styles/tokens.css';
import './styles/base.css';
import './styles/app.css';

// Set the theme attribute before the tree paints, so a dark-mode reader never sees a light flash.
initTheme();

const container = document.getElementById('root');
if (!container) throw new Error('Missing #root');

createRoot(container).render(
  <StrictMode>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </StrictMode>,
);
