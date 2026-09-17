import { render, waitFor } from '@testing-library/react';
import scaffolderPlugin from '@backstage/plugin-scaffolder/alpha';
import fs from 'fs';
import path from 'path';
import App, { appFeatures } from './App';

/**
 * Every extension id contributed by the app's registered features.
 *
 * The Scaffolder frontend plugin contributes `page:scaffolder`; the sidebar nav
 * module consumes that same id. Asserting on the registered extensions proves
 * the app can actually satisfy the navigation contract instead of only matching
 * a string in the source.
 */
function featureExtensionIds(): Array<string | undefined> {
  return appFeatures.flatMap(feature =>
    ((feature as { extensions?: Array<{ id?: string }> }).extensions ?? []).map(
      extension => extension.id,
    ),
  );
}

describe('App', () => {
  it('should render', async () => {
    process.env = {
      NODE_ENV: 'test',
      APP_CONFIG: [
        {
          data: {
            app: { title: 'Test' },
            backend: { baseUrl: 'http://localhost:7007' },
            techdocs: {
              storageUrl: 'http://localhost:7007/api/techdocs/static/docs',
            },
          },
          context: 'test',
        },
      ] as any,
    };

    const rendered = render(App.createRoot());

    await waitFor(() => {
      expect(rendered.baseElement).toBeInTheDocument();
    });
  });

  // Regression: the Golden Path Template was registered in the catalog, but the
  // app never registered the scaffolder frontend plugin, so the sidebar had no
  // Create entry and the template could not be executed from the UI.
  it('registers the Scaffolder frontend plugin', () => {
    expect(appFeatures).toContain(scaffolderPlugin);
  });

  it('registers a feature that contributes the scaffolder page', () => {
    expect(featureExtensionIds()).toContain('page:scaffolder');
  });

  it('satisfies the sidebar scaffolder navigation contract', () => {
    const sidebar = fs.readFileSync(
      path.join(__dirname, 'modules', 'nav', 'Sidebar.tsx'),
      'utf8',
    );

    expect(sidebar).toMatch(/nav\.take\(\s*'page:scaffolder'\s*\)/);
    expect(featureExtensionIds()).toContain('page:scaffolder');
  });

  it('keeps the non-scaffolder application features', () => {
    const ids = featureExtensionIds();

    for (const extensionId of [
      'page:catalog',
      'page:search',
      'page:notifications',
    ]) {
      expect(ids).toContain(extensionId);
    }
  });
});
