import * as fuzzysort from "fuzzysort"
import { createMemo, createSignal, onMount } from "solid-js"
import { useData } from "../context/data"
import { useLocal } from "../context/local"
import { useLocation } from "../context/location"
import { modelPreferenceKey } from "../model-preference"
import { DIALOG_LIST_INSET } from "../ui/dialog"
import { DialogSelect } from "../ui/dialog-select"
import { useTheme } from "../context/theme"
import { FavoriteMarker, ModelDetail, isFreeModel, prioritizeFavorites } from "./model-options"

/** Model picker for users without a connected provider, under the "new-designs" experiment. */
export function DialogFreeModels(props: {
  onSelect: (providerID: string, modelID: string) => void
  onConnect: () => void
}) {
  const local = useLocal()
  const data = useData()
  const location = useLocation()
  const theme = useTheme().surface("dialog")
  const [query, setQuery] = createSignal("")
  const modelList = createMemo(() => data.location.model.list(location.ref))
  onMount(() => {
    // An interrupted or failed location sync leaves the catalog missing; retry it while the user looks.
    if (modelList() === undefined) void data.location.sync(location.ref)
  })
  const emptyView = createMemo(() => (
    <box paddingLeft={DIALOG_LIST_INSET} paddingRight={DIALOG_LIST_INSET}>
      <text fg={theme.text.feedback.warning.base}>
        {modelList() === undefined ? "Loading models…" : "No free models available in this workspace"}
      </text>
    </box>
  ))
  // Favoriting does not reorder the list until the dialog is reopened.
  const favoritePriority = new Set(local.model.favorite().map(modelPreferenceKey))
  const recentRank = new Map(local.model.recent().map((item, index) => [modelPreferenceKey(item), index]))
  const isFavorite = (value: { providerID: string; modelID: string } | undefined) =>
    value !== undefined &&
    local.model.favorite().some((item) => item.providerID === value.providerID && item.modelID === value.modelID)

  const options = createMemo(() => {
    const all = (modelList() ?? [])
      .filter((model) => model.status !== "deprecated" && isFreeModel(model))
      .map((model) => {
        const value = { providerID: model.providerID, modelID: model.id }
        return {
          value,
          title: model.name,
          footer: isFavorite(value) ? <FavoriteMarker /> : undefined,
          detail: () => <ModelDetail model={model} price={false} />,
          onSelect: () => props.onSelect(model.providerID, model.id),
        }
      })
    const needle = query().trim()
    if (needle)
      return prioritizeFavorites(
        fuzzysort.go(needle, all, { key: "title" }).map((item) => item.obj),
        favoritePriority,
      )
    // Favorites first, then most recently used, then the server's order.
    const rank = (option: (typeof all)[number]) => recentRank.get(modelPreferenceKey(option.value)) ?? Infinity
    return prioritizeFavorites(
      all.toSorted((a, b) => rank(a) - rank(b)),
      favoritePriority,
    )
  })

  return (
    <DialogSelect
      title="Free models"
      description="Connect a provider for access to more models"
      options={options()}
      emptyView={emptyView()}
      actions={[
        {
          command: "model.dialog.provider",
          title: "connect provider",
          selection: "none",
          onTrigger: props.onConnect,
        },
        {
          command: "model.dialog.favorite",
          title: (option) => (isFavorite(option?.value) ? "unfavorite" : "favorite"),
          onTrigger: (option) => local.model.toggleFavorite(option.value),
        },
      ]}
      onFilter={setQuery}
      flat={true}
      skipFilter={true}
      current={local.model.current()}
    />
  )
}
