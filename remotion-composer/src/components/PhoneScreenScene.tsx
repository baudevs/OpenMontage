import {
  AbsoluteFill,
  Img,
  OffthreadVideo,
  interpolate,
  spring,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import { resolveAsset } from "../lib/resolveAsset";

export type PhoneScreenSceneProps = {
  /** Path to the source video (asset-manifest-resolved or public/-relative). */
  source: string;
  /** Seek position in seconds within the source clip. */
  sourceInSeconds?: number;
  /** Mute the source clip's own audio (default true — a separate music bed carries the mix). */
  muted?: boolean;
  /** Optional headline shown above the phone (e.g. a value-prop line). Omit for a clean cold open. */
  headline?: string;
  /** Optional small accent chip text shown just under the headline (e.g. a coin/bonus callout). */
  accentChip?: string;
  headlineColor?: string;
  accentColor?: string;
  bezelColor?: string;
  screenBackgroundColor?: string;
  /** Fill behind the phone itself (the space above/below a landscape phone). Defaults to black; pass 'transparent' when a backgroundImage/backgroundVideo is composited behind this scene. */
  canvasBackgroundColor?: string;
  /**
   * 'landscape' (default) draws the phone held sideways, screen aspect ~19.5:9.
   * Matches how these slot games are actually captured/played and lets the
   * full source frame show with `object-fit: contain` — no cropping.
   * 'portrait' is the legacy tall-phone treatment: object-fit: cover, which
   * center-crops wide source footage to fill a 9:19.5 screen. Only use
   * 'portrait' for genuinely portrait-shot source footage.
   */
  orientation?: "landscape" | "portrait";
  /** Optional spokescharacter cutout (transparent PNG) standing beside the phone. */
  characterSrc?: string;
  /** Which side the character stands on. Default 'right'. */
  characterSide?: "left" | "right";
  /** Character height as a fraction of canvas height. Default 0.56. */
  characterHeightPct?: number;
};

/**
 * Frames a real app-footage clip inside a code-drawn phone bezel — the
 * "screen recording shown on a phone" treatment for hybrid ad concepts that
 * anchor on real gameplay/UI capture rather than a talking head or B-roll.
 * Default landscape orientation shows the complete source frame
 * (object-fit: contain) since these games are captured/played landscape;
 * portrait mode is a legacy fallback that crops via object-fit: cover.
 */
export const PhoneScreenScene: React.FC<PhoneScreenSceneProps> = ({
  source,
  sourceInSeconds = 0,
  muted = true,
  headline,
  accentChip,
  headlineColor = "#FFFFFF",
  accentColor = "#FFB347",
  bezelColor = "#0B0B0F",
  screenBackgroundColor = "#000000",
  canvasBackgroundColor = "#000000",
  orientation = "landscape",
  characterSrc,
  characterSide = "right",
  characterHeightPct = 0.56,
}) => {
  const frame = useCurrentFrame();
  const { fps, width, height } = useVideoConfig();
  const isLandscape = orientation === "landscape";

  const phoneEntrance = spring({
    frame,
    fps,
    config: { damping: 16, stiffness: 90, mass: 0.9 },
  });

  const headlineSpring = spring({
    frame: frame - fps * 0.15,
    fps,
    config: { damping: 18, stiffness: 140 },
  });

  const characterEntrance = spring({
    frame: frame - fps * 0.1,
    fps,
    config: { damping: 15, stiffness: 80, mass: 1 },
  });

  // Landscape: phone laid on its side, screen ~19.5:9, sized off canvas
  // width so the full (wide) source clip fits via `contain` with minimal
  // letterboxing. Portrait (legacy): tall 9:19.5 device sized off height,
  // relies on `cover` to fill — crops wide source footage.
  const phoneWidth = isLandscape
    ? width * 0.86
    : Math.min(width * 0.82, height * 0.46);
  const phoneHeight = isLandscape
    ? phoneWidth * (9 / 19.5)
    : phoneWidth * (19.5 / 9);
  const bezelPx = Math.max(10, Math.min(phoneWidth, phoneHeight) * 0.035);
  const cornerRadius = Math.min(phoneWidth, phoneHeight) * 0.14;

  return (
    <AbsoluteFill style={{ backgroundColor: canvasBackgroundColor }}>
      <AbsoluteFill
        style={{
          justifyContent: "center",
          alignItems: "center",
          transform: `scale(${interpolate(phoneEntrance, [0, 1], [0.92, 1])})`,
          opacity: phoneEntrance,
        }}
      >
        <div
          style={{
            width: phoneWidth,
            height: phoneHeight,
            borderRadius: cornerRadius,
            background: bezelColor,
            padding: bezelPx,
            boxShadow:
              "0 30px 80px rgba(0,0,0,0.55), 0 0 0 1px rgba(255,255,255,0.06)",
            display: "flex",
          }}
        >
          <div
            style={{
              position: "relative",
              width: "100%",
              height: "100%",
              borderRadius: cornerRadius * 0.72,
              overflow: "hidden",
              background: screenBackgroundColor,
            }}
          >
            <OffthreadVideo
              src={resolveAsset(source)}
              startFrom={Math.round(sourceInSeconds * fps)}
              muted={muted}
              style={{
                position: "absolute",
                top: 0,
                left: 0,
                width: "100%",
                height: "100%",
                objectFit: isLandscape ? "contain" : "cover",
                objectPosition: "center",
                backgroundColor: screenBackgroundColor,
              }}
            />
            {isLandscape ? (
              /* Rotated-phone camera cutout, left edge */
              <div
                style={{
                  position: "absolute",
                  top: "50%",
                  left: phoneHeight * 0.04,
                  transform: "translateY(-50%)",
                  width: phoneHeight * 0.045,
                  height: phoneHeight * 0.22,
                  borderRadius: phoneHeight * 0.025,
                  background: "rgba(0,0,0,0.55)",
                }}
              />
            ) : (
              /* Simulated status-bar chrome so the crop reads as "a phone screen" */
              <div
                style={{
                  position: "absolute",
                  top: 0,
                  left: 0,
                  right: 0,
                  height: phoneWidth * 0.09,
                  background:
                    "linear-gradient(to bottom, rgba(0,0,0,0.45), transparent)",
                  display: "flex",
                  justifyContent: "center",
                  alignItems: "flex-start",
                  paddingTop: phoneWidth * 0.015,
                }}
              >
                <div
                  style={{
                    width: phoneWidth * 0.22,
                    height: phoneWidth * 0.045,
                    borderRadius: phoneWidth * 0.025,
                    background: "rgba(0,0,0,0.55)",
                  }}
                />
              </div>
            )}
          </div>
        </div>
      </AbsoluteFill>

      {characterSrc && (
        <AbsoluteFill
          style={{
            justifyContent: "flex-end",
            alignItems: characterSide === "right" ? "flex-end" : "flex-start",
          }}
        >
          <Img
            src={resolveAsset(characterSrc)}
            style={{
              height: height * characterHeightPct,
              width: "auto",
              [characterSide === "right" ? "marginRight" : "marginLeft"]:
                width * 0.02,
              opacity: characterEntrance,
              transform: `translateY(${interpolate(characterEntrance, [0, 1], [40, 0])}px)`,
              filter: "drop-shadow(0 20px 30px rgba(0,0,0,0.45))",
            }}
          />
        </AbsoluteFill>
      )}

      {headline && (
        <AbsoluteFill
          style={{
            justifyContent: "flex-start",
            alignItems: "center",
            paddingTop: height * 0.1,
          }}
        >
          {accentChip && (
            <div
              style={{
                opacity: headlineSpring,
                transform: `translateY(${interpolate(headlineSpring, [0, 1], [-14, 0])}px)`,
                background: accentColor,
                color: "#1A1200",
                fontFamily: "Inter, system-ui, sans-serif",
                fontWeight: 800,
                fontSize: width * 0.032,
                padding: `${width * 0.012}px ${width * 0.03}px`,
                borderRadius: 999,
                marginBottom: width * 0.025,
                letterSpacing: "0.02em",
              }}
            >
              {accentChip}
            </div>
          )}
          <div
            style={{
              opacity: headlineSpring,
              transform: `translateY(${interpolate(headlineSpring, [0, 1], [16, 0])}px)`,
              color: headlineColor,
              fontFamily: "Inter, system-ui, sans-serif",
              fontWeight: 800,
              fontSize: width * 0.062,
              textAlign: "center",
              maxWidth: "84%",
              lineHeight: 1.15,
              WebkitTextStroke: "6px #7A0E0E",
              paintOrder: "stroke fill",
              textShadow: "0 6px 18px rgba(0,0,0,0.5)",
            }}
          >
            {headline}
          </div>
        </AbsoluteFill>
      )}
    </AbsoluteFill>
  );
};
