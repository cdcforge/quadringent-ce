import { useState, type KeyboardEvent, type ReactNode } from 'react';

/**
 * BandedTable — liste de type « papier listing » : bandes alternées vert très
 * pâle (papier continu d'imprimante), chiffres en mono alignés à droite,
 * filets discrets. Chaque ligne est focalisable au clavier (flèches haut/bas)
 * pour naviguer sans souris, avec un emplacement d'actions par ligne.
 */
export interface BandedTableColumn<Row> {
  readonly key: string;
  readonly header: string;
  /** Colonnes numériques : mono, alignées à droite, `tabular-nums`. */
  readonly numeric?: boolean;
  readonly render: (row: Row) => ReactNode;
}

export interface BandedTableProps<Row> {
  readonly caption: string;
  readonly columns: readonly BandedTableColumn<Row>[];
  readonly rows: readonly Row[];
  readonly rowKey: (row: Row) => string;
  /** Emplacement d'actions rendu en dernière colonne pour la ligne donnée. */
  readonly rowActions?: (row: Row) => ReactNode;
}

export function BandedTable<Row>({ caption, columns, rows, rowKey, rowActions }: BandedTableProps<Row>) {
  const [focusedIndex, setFocusedIndex] = useState<number>(0);

  const onRowKeyDown = (event: KeyboardEvent<HTMLTableRowElement>, index: number) => {
    if (event.key === 'ArrowDown' && index < rows.length - 1) {
      event.preventDefault();
      setFocusedIndex(index + 1);
      focusRow(event.currentTarget, index + 1);
    } else if (event.key === 'ArrowUp' && index > 0) {
      event.preventDefault();
      setFocusedIndex(index - 1);
      focusRow(event.currentTarget, index - 1);
    }
  };

  return (
    <table className="banded-table">
      <caption className="sr-only">{caption}</caption>
      <thead>
        <tr>
          {columns.map((column) => (
            <th key={column.key} scope="col" className={column.numeric ? 'banded-table__col--numeric' : undefined}>
              {column.header}
            </th>
          ))}
          {rowActions ? <th scope="col" className="sr-only">Actions</th> : null}
        </tr>
      </thead>
      <tbody>
        {rows.map((row, index) => (
          <tr
            key={rowKey(row)}
            className="banded-table__row"
            tabIndex={index === focusedIndex ? 0 : -1}
            onFocus={() => setFocusedIndex(index)}
            onKeyDown={(event) => onRowKeyDown(event, index)}
          >
            {columns.map((column) => (
              <td
                key={column.key}
                className={column.numeric ? 'banded-table__col--numeric mono' : undefined}
              >
                {column.render(row)}
              </td>
            ))}
            {rowActions ? <td className="banded-table__col--actions">{rowActions(row)}</td> : null}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function focusRow(current: HTMLTableRowElement, index: number) {
  const table = current.closest('table');
  const target = table?.querySelectorAll('tbody tr')[index] as HTMLElement | undefined;
  target?.focus();
}
