import { useEffect, useState } from "react";
import { Download, ImageOff } from "lucide-react";
import type { ChatImageResult, ImageArtifact } from "./api";

export function StoredImage({
  token,
  characterId,
  image,
  alt,
  download = false,
}: {
  token: string;
  characterId: string;
  image: ImageArtifact;
  alt: string;
  download?: boolean;
}) {
  const [url, setUrl] = useState("");
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    let objectUrl = "";
    setUrl("");
    setFailed(false);
    fetch(
      "/media/" +
        encodeURIComponent(image.artifact) +
        "?" +
        new URLSearchParams({ character_id: characterId }),
      {
        headers: { Authorization: "Bearer " + token },
        signal: controller.signal,
      },
    )
      .then(async (response) => {
        if (!response.ok) throw new Error("Image unavailable");
        const blob = await response.blob();
        if (controller.signal.aborted) return;
        objectUrl = URL.createObjectURL(blob);
        setUrl(objectUrl);
      })
      .catch(() => {
        if (!controller.signal.aborted) setFailed(true);
      });
    return () => {
      controller.abort();
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [token, characterId, image.artifact]);
  if (failed)
    return (
      <div className="image-unavailable">
        <ImageOff size={20} /> Image unavailable.
      </div>
    );
  if (!url)
    return (
      <div className="image-placeholder" role="status">
        Loading image…
      </div>
    );
  return (
    <>
      <img src={url} alt={alt} loading="lazy" onError={() => setFailed(true)} />
      {download && (
        <figcaption>
          <span>Generated image</span>
          <a
            href={url}
            download={image.artifact}
            aria-label="Download generated image"
          >
            <Download size={15} /> Save
          </a>
        </figcaption>
      )}
    </>
  );
}

export function ReplyImages({
  token,
  characterId,
  result,
  onAppearance,
}: {
  token: string;
  characterId: string;
  result?: ChatImageResult | null;
  onAppearance: () => void;
}) {
  if (!result) return null;
  if (result.status !== "succeeded")
    return (
      <div className="image-error" role="status">
        <ImageOff size={18} />
        <div>
          <p>{result.error || "The image could not be generated."}</p>
          {result.status === "needs_appearance" && (
            <button
              type="button"
              className="text-button"
              onClick={onAppearance}
            >
              Add character appearance
            </button>
          )}
        </div>
      </div>
    );
  return (
    <div className="reply-images">
      {result.artifacts?.map((image) => (
        <figure key={image.artifact}>
          <StoredImage
            token={token}
            characterId={characterId}
            image={image}
            alt="Generated character image"
            download
          />
        </figure>
      ))}
    </div>
  );
}
