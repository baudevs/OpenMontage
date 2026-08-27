import {
  AbsoluteFill,
  OffthreadVideo,
  interpolate,
  spring,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import { resolveAsset } from "../lib/resolveAsset";

export type ScreenCompositeSceneProps = {
  /**
   * A pre-composited, full-bleed video: real app footage already
   * chroma-key-composited (via ffmpeg colorkey+despill) onto a hand+phone
   * plate at the source level. This component just plays it back and
   * overlays an optional native caption — it does NOT do any in-Remotion
   * positioning/overlay math. Real per-pixel chroma-keying (not a fixed CSS
   * overlay) is what makes the phone bezel visible and keeps the composite
   * aligned even though the source plate has tiny natural hand/camera
   * micro-movement — a fixed-position overlay can't track that, a
   * pixel-level key naturally does.
   */
  source: string;
  /** Seek position in seconds within the pre-composited source video. */
  sourceInSeconds?: number;
  muted?: boolean;
  /** Native lower-third caption text. Omit for a clean, caption-free beat. */
  caption?: string;
  captionColor?: string;
};

/**
 * Plays a pre-composited (real chroma-key, not CSS overlay) app-footage clip
 * full-bleed, with an optional plain native-style lower-third caption. The
 * "native/organic" counterpart to PhoneScreenScene's overtly branded
 * phone-frame treatment — no marquee headline, just a understated caption.
 */
export const ScreenCompositeScene: React.FC<ScreenCompositeSceneProps> = ({
  source,
  sourceInSeconds = 0,
  muted = true,
  caption,
  captionColor = "#FFFFFF",
}) => {
  const frame = useCurrentFrame();
  const { fps, width, height } = useVideoConfig();

  const captionSpring = spring({
    frame: frame - fps * 0.1,
    fps,
    config: { damping: 20, stiffness: 120 },
  });

  return (
    <AbsoluteFill style={{ backgroundColor: "#000000" }}>
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
          objectFit: "cover",
        }}
      />

      {caption && (
        <AbsoluteFill
          style={{
            justifyContent: "flex-start",
            alignItems: "center",
            paddingTop: height * 0.79,
          }}
        >
          <div
            style={{
              opacity: captionSpring,
              transform: `translateY(${interpolate(captionSpring, [0, 1], [16, 0])}px)`,
              background: "rgba(0,0,0,0.55)",
              color: captionColor,
              fontFamily: "Inter, system-ui, sans-serif",
              fontWeight: 500,
              fontSize: width * 0.038,
              textAlign: "center",
              lineHeight: 1.3,
              maxWidth: "82%",
              padding: `${height * 0.012}px ${width * 0.045}px`,
              borderRadius: 14,
            }}
          >
            {caption}
          </div>
        </AbsoluteFill>
      )}
    </AbsoluteFill>
  );
};
