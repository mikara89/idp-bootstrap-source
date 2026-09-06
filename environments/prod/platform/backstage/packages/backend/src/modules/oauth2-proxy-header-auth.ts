import { createBackendModule } from '@backstage/backend-plugin-api';
import {
  authProvidersExtensionPoint,
  commonSignInResolvers,
  createProxyAuthProviderFactory,
  createProxyAuthenticator,
  type ProfileInfo,
} from '@backstage/plugin-auth-node';

type ForwardAuthHeaders = {
  getHeader(name: string): string | undefined;
};

type ForwardAuthResult = ForwardAuthHeaders;

/**
 * Translates only the identity headers copied by Traefik after OAuth2 Proxy
 * ForwardAuth. The Backstage service must remain reachable only through that
 * ingress boundary; these headers are not safe on a directly exposed route.
 */
export function profileFromForwardAuthHeaders(
  headers: ForwardAuthHeaders,
): ProfileInfo {
  const email = headers.getHeader('x-auth-request-email');
  const displayName =
    headers.getHeader('x-auth-request-preferred-username') ||
    headers.getHeader('x-auth-request-user');

  if (!email) {
    throw new Error('ForwardAuth response did not contain X-Auth-Request-Email');
  }
  if (!displayName) {
    throw new Error(
      'ForwardAuth response did not contain an X-Auth-Request username',
    );
  }

  return { email, displayName };
}

const oauth2ProxyForwardAuthAuthenticator = createProxyAuthenticator<
  undefined,
  ForwardAuthResult,
  undefined
>({
  defaultProfileTransform: async result => ({
    profile: profileFromForwardAuthHeaders(result),
  }),
  initialize() {
    return undefined;
  },
  async authenticate({ req }) {
    const result: ForwardAuthResult = {
      getHeader(name) {
        return req.get(name);
      },
    };

    return { result };
  },
});

export default createBackendModule({
  pluginId: 'auth',
  moduleId: 'oauth2-proxy-forward-auth-headers',
  register(reg) {
    reg.registerInit({
      deps: { providers: authProvidersExtensionPoint },
      async init({ providers }) {
        providers.registerProvider({
          providerId: 'oauth2Proxy',
          factory: createProxyAuthProviderFactory({
            authenticator: oauth2ProxyForwardAuthAuthenticator,
            signInResolverFactories: commonSignInResolvers,
          }),
        });
      },
    });
  },
});
