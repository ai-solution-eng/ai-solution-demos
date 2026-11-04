import { createRemoteJWKSet, jwtVerify, type JWTPayload } from "jose";

// Who may change System Config. Behind PCAI's ingress, oauth2-proxy forwards the signed-in
// user's Keycloak ID token as a bearer token. It is verified here against Keycloak's keys,
// never trusted as sent, so a forged header from inside the cluster gets nowhere.
const SETTINGS_AUTH = process.env.SETTINGS_AUTH ?? "oidc"; // "open" only for the Tilt dev loop
const OIDC_ISSUER = (process.env.OIDC_ISSUER ?? "").replace(/\/+$/, "");
const OIDC_AUDIENCE = process.env.OIDC_AUDIENCE ?? "ua";
// Where to fetch Keycloak's signing keys. The chart points it at Keycloak's in-cluster Service, as
// PCAI's own oauth2-proxy does, so a self-signed ingress certificate does not break the check.
const OIDC_JWKS_URL = process.env.OIDC_JWKS_URL || `${OIDC_ISSUER}/protocol/openid-connect/certs`;
const ADMIN_ROLE = process.env.ADMIN_ROLE ?? "admin";

let jwks: ReturnType<typeof createRemoteJWKSet> | null = null;

export interface SettingsAccess {
  canEdit: boolean;
  user: string | null;
  reason: string | null;
}

function claimList(payload: JWTPayload, name: string): string[] {
  const value = payload[name];
  return Array.isArray(value) ? value.filter((v): v is string => typeof v === "string") : [];
}

function roles(payload: JWTPayload): string[] {
  // PCAI's realm maps realm roles into `groups`; standard Keycloak tokens carry realm_access.
  const realmAccess = payload.realm_access;
  const realmRoles =
    typeof realmAccess === "object" && realmAccess !== null && "roles" in realmAccess
      ? claimList(realmAccess as JWTPayload, "roles")
      : [];
  return [...claimList(payload, "groups"), ...realmRoles];
}

export async function settingsAccess(headers: Headers): Promise<SettingsAccess> {
  if (SETTINGS_AUTH === "open") {
    return { canEdit: true, user: null, reason: null };
  }
  if (!OIDC_ISSUER) {
    return { canEdit: false, user: null, reason: "OIDC_ISSUER is not set, so administrators cannot be recognised." };
  }
  const token = headers.get("authorization")?.match(/^Bearer\s+(\S+)$/i)?.[1];
  if (!token) {
    return { canEdit: false, user: null, reason: "No sign-in token reached the app, so settings are read-only." };
  }
  try {
    jwks ??= createRemoteJWKSet(new URL(OIDC_JWKS_URL));
    const { payload } = await jwtVerify(token, jwks, { issuer: OIDC_ISSUER, audience: OIDC_AUDIENCE });
    const user =
      (typeof payload.preferred_username === "string" ? payload.preferred_username : null) ?? payload.sub ?? null;
    if (roles(payload).includes(ADMIN_ROLE)) {
      return { canEdit: true, user, reason: null };
    }
    return { canEdit: false, user, reason: `Only PCAI administrators (role ${ADMIN_ROLE}) can change these settings.` };
  } catch (err) {
    const detail = err instanceof Error ? err.message : String(err);
    return { canEdit: false, user: null, reason: `Your sign-in token could not be verified (${detail}).` };
  }
}
