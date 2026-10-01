import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import App from './App';
import './styles/product-tokens.css';
import './styles/base.css';
import './styles/brand.css';
import './styles/product-shell.css';
import './styles/foundation.css';
import './styles/wizard.css';
import './styles/responsive.css';
import './styles/commercial.css';

// Page de référence des fondations visuelles, réservée au développement.
// `import.meta.env.DEV` est remplacé par une constante au build : la branche
// production ne référence jamais `FoundationReference`, qui n'est donc pas
// incluse dans le contrat produit ni dans le bundle expédié.
const showFoundationReference = import.meta.env.DEV && window.location.hash.startsWith('#/_fondation');

async function render() {
  const root = createRoot(document.getElementById('root')!);
  if (showFoundationReference) {
    const { FoundationReference } = await import('./dev/FoundationReference.tsx');
    root.render(
      <StrictMode>
        <FoundationReference />
      </StrictMode>,
    );
    return;
  }
  root.render(
    <StrictMode>
      <App />
    </StrictMode>,
  );
}

void render();
