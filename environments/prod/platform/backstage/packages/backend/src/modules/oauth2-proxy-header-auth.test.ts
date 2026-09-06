import { profileFromForwardAuthHeaders } from './oauth2-proxy-header-auth';

function headers(values: Record<string, string | undefined>) {
  return {
    getHeader(name: string) {
      return values[name.toLowerCase()];
    },
  };
}

describe('profileFromForwardAuthHeaders', () => {
  it('uses the ForwardAuth email and preferred username', () => {
    expect(
      profileFromForwardAuthHeaders(
        headers({
          'x-auth-request-email': 'user@example.com',
          'x-auth-request-preferred-username': 'user',
          'x-auth-request-user': 'fallback-user',
          'x-auth-request-groups': 'platform-users',
        }),
      ),
    ).toEqual({ email: 'user@example.com', displayName: 'user' });
  });

  it('falls back to the ForwardAuth user header for display name', () => {
    expect(
      profileFromForwardAuthHeaders(
        headers({
          'x-auth-request-email': 'user@example.com',
          'x-auth-request-user': 'user',
        }),
      ),
    ).toEqual({ email: 'user@example.com', displayName: 'user' });
  });

  it('fails closed when ForwardAuth did not provide an email', () => {
    expect(() =>
      profileFromForwardAuthHeaders(
        headers({ 'x-auth-request-user': 'user' }),
      ),
    ).toThrow('X-Auth-Request-Email');
  });
});
