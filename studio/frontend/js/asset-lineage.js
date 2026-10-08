/** Pure function module for data asset lineage analysis and source/derived relationship grouping. */
const QuicDataAssetLineage = (() => {
  const MAX_DEPTH = 32;
  const KINDS = new Set(['source', 'derived']);
  const REVIEW_STATUSES = new Set(['pending', 'accepted', 'rejected', 'not_applicable']);
  const PUBLICATION_STATUSES = new Set(['unpublished', 'publishing', 'published', 'failed']);

  function idOf(item, index) {
    const value = item?.id;
    return value === null || value === undefined || value === '' ? `invalid-${index}` : value;
  }

  function cloneItem(item) {
    const copy = { ...item };
    if (item?.quality && typeof item.quality === 'object') copy.quality = { ...item.quality };
    if (item?.publication && typeof item.publication === 'object') copy.publication = { ...item.publication };
    return copy;
  }

  function filterValue(filters, key) {
    const value = filters?.[key];
    return typeof value === 'string' ? value.trim() : value;
  }

  function validFilters(filters) {
    const kind = filterValue(filters, 'kind');
    const review = filterValue(filters, 'review_status');
    const publication = filterValue(filters, 'publication_status');
    return (!kind || KINDS.has(kind))
      && (!review || REVIEW_STATUSES.has(review))
      && (!publication || PUBLICATION_STATUSES.has(publication));
  }

  function hasActiveFilters(filters) {
    return ['kind', 'review_status', 'publication_status'].some((key) => Boolean(filterValue(filters, key)));
  }

  function matches(item, filters) {
    const kind = filterValue(filters, 'kind');
    const review = filterValue(filters, 'review_status');
    const publication = filterValue(filters, 'publication_status');
    const category = publicationCategory(item);
    return (!kind || item.kind === kind)
      && (!review || item.review_status === review)
      && (!publication || category === publication);
  }

  function publicationCategory(item) {
    const category = item?.publication?.category;
    if (PUBLICATION_STATUSES.has(category)) return category;
    if (item?.publication?.status === 'succeeded') return 'published';
    if (item?.publication?.status === 'failed') return 'failed';
    if (['queued', 'running', 'retry_pending'].includes(item?.publication?.status)) return 'publishing';
    return 'unpublished';
  }

  function summaryOf(root) {
    const summary = {
      descendants: 0,
      quality_failed: 0,
      review: {},
      publication: {},
    };
    const visit = (node) => {
      for (const child of node.children) {
        summary.descendants += 1;
        const review = child.item.review_status;
        if (REVIEW_STATUSES.has(review) && review !== 'not_applicable') {
          summary.review[review] = (summary.review[review] || 0) + 1;
        }
        if (child.item.quality?.status === 'failed' || child.item.quality_status === 'failed') {
          summary.quality_failed += 1;
        }
        const publication = publicationCategory(child.item);
        summary.publication[publication] = (summary.publication[publication] || 0) + 1;
        visit(child);
      }
    };
    visit(root);
    return summary;
  }

  function flatten(nodes, result = []) {
    for (const node of nodes) {
      result.push(node.item);
      flatten(node.children, result);
    }
    return result;
  }

  function rangeStartOf(root) {
    let earliest = null;
    const visit = (node) => {
      for (const child of node.children) {
        try {
          const value = String(child.item.source_start_ns ?? '');
          const timestamp = /^\d+$/.test(value) ? BigInt(value) : null;
          if (timestamp !== null && (earliest === null || timestamp < earliest)) earliest = timestamp;
        } catch {
          // Invalid timestamps remain observable on the row as an unavailable range.
        }
        visit(child);
      }
    };
    visit(root);
    return earliest === null ? null : earliest.toString();
  }

  function project(node, filters, active) {
    const children = node.children
      .map((child) => project(child, filters, active))
      .filter(Boolean);
    if (!active || matches(node.item, filters) || children.length) {
      return {
        ...node,
        item: cloneItem(node.item),
        children,
      };
    }
    return null;
  }

  function buildGroups(items, filters = {}) {
    if (!Array.isArray(items) || !validFilters(filters)) return [];
    const nodes = new Map();
    const ordered = [];
    items.forEach((item, index) => {
      if (!item || typeof item !== 'object') return;
      const id = idOf(item, index);
      const key = String(id);
      if (nodes.has(key)) return;
      const node = {
        id,
        key,
        item: cloneItem(item),
        children: [],
        parent: null,
        issue: idOf(item, index) === `invalid-${index}` ? 'invalid_id' : null,
      };
      nodes.set(key, node);
      ordered.push(node);
    });

    for (const node of ordered) {
      if (node.item.kind === 'source') {
        if (node.item.parent_episode_id !== null && node.item.parent_episode_id !== undefined) {
          node.issue = 'unexpected_parent';
        }
        continue;
      }
      const parentId = node.item.parent_episode_id;
      const parent = nodes.get(String(parentId));
      if (!parent || parent === node) {
        node.issue = parent === node ? 'cycle' : 'missing_parent';
      } else {
        node.parent = parent;
      }
    }

    // Detach every member of a cycle so corrupt ancestry cannot hide assets.
    for (const start of ordered) {
      const path = [];
      const positions = new Map();
      let current = start;
      while (current?.parent) {
        if (positions.has(current.key)) {
          const cycle = path.slice(positions.get(current.key));
          cycle.forEach((node) => {
            node.parent = null;
            node.issue = 'cycle';
          });
          break;
        }
        positions.set(current.key, path.length);
        path.push(current);
        current = current.parent;
      }
    }

    for (const node of ordered) {
      let current = node;
      let depth = 0;
      while (current.parent && depth < MAX_DEPTH) {
        current = current.parent;
        depth += 1;
      }
      if (current.parent) {
        node.parent = null;
        node.issue = 'depth_exceeded';
      }
    }

    for (const node of ordered) node.children = [];
    const roots = [];
    for (const node of ordered) {
      if (node.parent) node.parent.children.push(node);
      else roots.push(node);
    }

    const active = hasActiveFilters(filters);
    return roots.map((root) => {
      const tree = project(root, filters, active);
      if (!tree) return null;
      const summary = summaryOf(root);
      return {
        root: tree.item,
        tree,
        descendants: flatten(tree.children),
        summary,
        range_start_ns: rangeStartOf(root),
        auto_expand: active && tree.children.length > 0,
        lineage_issue: root.issue,
      };
    }).filter(Boolean);
  }

  function isExpanded(expanded, id) {
    return expanded.has(id) || expanded.has(String(id));
  }

  function visibleRows(groups, expandedIds) {
    const expanded = expandedIds && typeof expandedIds.has === 'function' ? expandedIds : new Set();
    const rows = [];
    const append = (node, depth, group, forceExpand) => {
      rows.push({
        ...cloneItem(node.item),
        asset_depth: depth,
        asset_group_id: group.root.id,
        asset_has_children: node.children.length > 0,
        asset_lineage_issue: depth === 0 ? group.lineage_issue : node.issue,
        asset_summary: depth === 0 ? group.summary : null,
        asset_group_start_ns: group.range_start_ns,
      });
      if (!node.children.length || (!forceExpand && !isExpanded(expanded, node.item.id))) return;
      node.children.forEach((child) => append(child, depth + 1, group, forceExpand));
    };
    groups.forEach((group) => append(group.tree, 0, group, group.auto_expand));
    return rows;
  }

  function selectionRows(items, selectedIds) {
    const selected = new Set((Array.isArray(selectedIds) ? selectedIds : []).map((id) => String(id)));
    if (!selected.size) return [];
    const rows = [];
    const projectSelection = (node) => {
      const children = node.children.map(projectSelection).filter(Boolean);
      if (!selected.has(String(node.item.id)) && !children.length) return null;
      return { ...node, children };
    };
    const append = (node, depth, group) => {
      rows.push({
        ...cloneItem(node.item),
        asset_depth: depth,
        asset_selected: selected.has(String(node.item.id)),
        asset_has_children: node.children.length > 0,
        asset_lineage_issue: depth === 0 ? group.lineage_issue : node.issue,
      });
      node.children.forEach((child) => append(child, depth + 1, group));
    };
    buildGroups(items).forEach((group) => {
      const tree = projectSelection(group.tree);
      if (tree) append(tree, 0, group);
    });
    return rows;
  }

  return Object.freeze({ buildGroups, visibleRows, selectionRows, publicationCategory });
})();
