export type TrustAction =
  | "bridgeHealth"
  | "configureProvider"
  | "runDoctor"
  | "readonlyPlaceholder"
  | "mutatingAction";

export interface TrustGateResult {
  allowed: boolean;
  reason?: string;
}

const TRUST_REQUIRED_MESSAGE =
  "This Alysis Code action requires Workspace Trust because it may lead to file writes, shell commands, or project-local capability loading.";

export function evaluateWorkspaceTrust(isTrusted: boolean, action: TrustAction): TrustGateResult {
  if (isTrusted) {
    return { allowed: true };
  }

  switch (action) {
    case "bridgeHealth":
    case "configureProvider":
    case "runDoctor":
    case "readonlyPlaceholder":
      return { allowed: true };
    case "mutatingAction":
      return {
        allowed: false,
        reason: TRUST_REQUIRED_MESSAGE
      };
  }
}
