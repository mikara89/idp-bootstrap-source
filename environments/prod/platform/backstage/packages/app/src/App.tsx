import { configApiRef, useApi } from '@backstage/core-plugin-api';
import { ProxiedSignInPage, SignInPage } from '@backstage/core-components';
import {
  PageBlueprint,
  createFrontendModule,
  createRouteRef,
} from '@backstage/frontend-plugin-api';
import { createApp } from '@backstage/frontend-defaults';
import catalogPlugin from '@backstage/plugin-catalog/alpha';
import searchPlugin from '@backstage/plugin-search/alpha';
import notificationsPlugin from '@backstage/plugin-notifications/alpha';
import signalsPlugin from '@backstage/plugin-signals/alpha';
import { SignInPageBlueprint } from '@backstage/plugin-app-react';
import { Navigate } from 'react-router-dom';
import { navModule } from './modules/nav';

const signInPage = SignInPageBlueprint.make({
  params: {
    loader: async () => props => {
      const configApi = useApi(configApiRef);
      const authEnvironment =
        configApi.getOptionalString('auth.environment') ?? 'production';

      if (authEnvironment === 'development') {
        return <SignInPage {...props} providers={['guest']} />;
      }

      return <ProxiedSignInPage {...props} provider="oauth2Proxy" />;
    },
  },
});

const rootRouteRef = createRouteRef();

const catalogRootRedirectPage = PageBlueprint.make({
  params: {
    path: '/',
    routeRef: rootRouteRef,
    noHeader: true,
    loader: async () => <Navigate to="/catalog" replace />,
  },
});

export default createApp({
  features: [
    catalogPlugin,
    searchPlugin,
    notificationsPlugin,
    signalsPlugin,
    navModule,
    createFrontendModule({
      pluginId: 'app',
      extensions: [catalogRootRedirectPage, signInPage],
    }),
  ],
});
