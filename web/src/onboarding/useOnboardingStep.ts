import { useCallback } from "react";

import { hasSeen, markSeen, skipAll, useOnboardingState } from "./client";
import { OnboardingStepId } from "./stepIds";

export type OnboardingStep = {
  ready: boolean;
  visible: boolean;
  dismiss: () => void;
  skipAll: () => void;
};

// Hook for spotlight beats that don't anchor to a DOM node (e.g. a modal-like
// tour card). Pair with <Coachmark/> when you do have an anchor.
export function useOnboardingStep(stepId: OnboardingStepId): OnboardingStep {
  const s = useOnboardingState();
  const dismiss = useCallback(() => {
    void markSeen(stepId);
  }, [stepId]);
  const onSkipAll = useCallback(() => {
    void skipAll();
  }, []);
  return {
    ready: s.loaded,
    visible: s.loaded && !hasSeen(stepId),
    dismiss,
    skipAll: onSkipAll,
  };
}
