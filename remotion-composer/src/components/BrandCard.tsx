import {
  AbsoluteFill,
  Img,
  interpolate,
  spring,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import { resolveAsset } from "../lib/resolveAsset";

export type BrandCardProps = {
  logoSrc: string;
  /** 'compliance' = logo + small legal/disclosure text. 'cta' = big headline + pill button. */
  variant: "compliance" | "cta";
  /** Compliance variant: the disclosure copy, shown verbatim. */
  bodyText?: string;
  /** CTA variant: the headline (e.g. "Download Slotpark free"). */
  headline?: string;
  /** CTA variant: the button label (e.g. "Free Install"). */
  buttonLabel?: string;
  accentColor?: string;
  backgroundColor?: string;
};

/**
 * Branded end-card: logo reveal plus either the mandatory compliance line
 * or the CTA button. Reused across every hybrid ad concept's outro beats.
 */
export const BrandCard: React.FC<BrandCardProps> = ({
  logoSrc,
  variant,
  bodyText,
  headline,
  buttonLabel,
  accentColor = "#FFB347",
  backgroundColor = "#170B10",
}) => {
  const frame = useCurrentFrame();
  const { fps, width, height } = useVideoConfig();

  const logoSpring = spring({
    frame,
    fps,
    config: { damping: 15, stiffness: 110, mass: 0.9 },
  });
  const bodySpring = spring({
    frame: frame - fps * 0.35,
    fps,
    config: { damping: 20 },
  });
  const buttonSpring = spring({
    frame: frame - fps * 0.55,
    fps,
    config: { damping: 13, stiffness: 160 },
  });
  const buttonPulse = 1 + Math.sin(Math.max(0, frame - fps * 1.2) * 0.15) * 0.03;

  return (
    <AbsoluteFill
      style={{
        background: backgroundColor,
        justifyContent: "center",
        alignItems: "center",
      }}
    >
      <div
        style={{
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          width: "84%",
        }}
      >
        <Img
          src={resolveAsset(logoSrc)}
          style={{
            width: width * 0.5,
            opacity: logoSpring,
            transform: `scale(${interpolate(logoSpring, [0, 1], [0.8, 1])}) translateY(${interpolate(logoSpring, [0, 1], [18, 0])}px)`,
            marginBottom: height * 0.035,
          }}
        />

        {variant === "compliance" && bodyText && (
          <div
            style={{
              opacity: bodySpring,
              background: "rgba(0,0,0,0.5)",
              padding: `${height * 0.014}px ${width * 0.045}px`,
              borderRadius: 14,
              maxWidth: "92%",
            }}
          >
            <div
              style={{
                color: "#FFFFFF",
                fontFamily: "Inter, system-ui, sans-serif",
                fontWeight: 700,
                fontSize: width * 0.032,
                textAlign: "center",
                lineHeight: 1.5,
                textShadow: "0 1px 3px rgba(0,0,0,0.6)",
              }}
            >
              {bodyText}
            </div>
          </div>
        )}

        {variant === "cta" && (
          <>
            {headline && (
              <div
                style={{
                  opacity: bodySpring,
                  transform: `translateY(${interpolate(bodySpring, [0, 1], [14, 0])}px)`,
                  color: "#FFFFFF",
                  fontFamily: "Inter, system-ui, sans-serif",
                  fontWeight: 800,
                  fontSize: width * 0.058,
                  textAlign: "center",
                  lineHeight: 1.15,
                  marginBottom: height * 0.035,
                }}
              >
                {headline}
              </div>
            )}
            {buttonLabel && (
              <div
                style={{
                  opacity: buttonSpring,
                  transform: `scale(${interpolate(buttonSpring, [0, 1], [0.85, 1]) * buttonPulse})`,
                  background: accentColor,
                  color: "#1A1200",
                  fontFamily: "Inter, system-ui, sans-serif",
                  fontWeight: 800,
                  fontSize: width * 0.04,
                  padding: `${height * 0.02}px ${width * 0.09}px`,
                  borderRadius: 999,
                  boxShadow: "0 12px 30px rgba(0,0,0,0.35)",
                  letterSpacing: "0.01em",
                }}
              >
                {buttonLabel}
              </div>
            )}
          </>
        )}
      </div>
    </AbsoluteFill>
  );
};
