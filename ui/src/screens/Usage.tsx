/**
 * Consommation — ce que la copie a coûté en ressources.
 *
 * Les relevés, montants calculés et absences gardent leur périmètre et leur date.
 */

import type { ControlPlaneState } from '../data/useControlPlane.ts';
import type { Overview as OverviewData, Pipeline } from '../domain/controlPlane.ts';
import { infrastructureCostsFor, snowflakeCostFor } from '../domain/operator.ts';
import { consumptionFor, hasMeasures } from '../domain/consumption.ts';
import '../styles/liaison.css';
import '../styles/consumption.css';

export function Usage({
  state,
  pipelines,
  onRefresh,
}: {
  readonly state: ControlPlaneState;
  readonly overview: OverviewData | null;
  readonly pipelines: readonly Pipeline[];
  readonly onRefresh: () => void;
}) {
  const ready = state.status !== 'loading' && state.status !== 'failed';

  return (
    <div className="board">
      <header className="board__head">
        <div>
          <h1 className="board__title">Consommation</h1>
          <p className="board__summary">Ce que vos copies ont consommé en ressources.</p>
        </div>
        <button type="button" className="board__refresh" onClick={onRefresh}>
          Actualiser
        </button>
      </header>

      {!ready || pipelines.length === 0 ? (
        <section className="board__empty">
          <h2>{ready ? 'Aucune liaison' : 'Lecture en cours'}</h2>
          <p>
            {ready
              ? 'La consommation apparaîtra dès qu’une liaison aura tourné.'
              : 'Quadringent interroge le service.'}
          </p>
        </section>
      ) : (
        <div className="board__list">
          {pipelines.map((pipeline) => {
            const view = consumptionFor(pipeline);
            const cost = snowflakeCostFor(pipeline);
            const infrastructure = infrastructureCostsFor(pipeline);
            return (
              <section className="consumption" key={pipeline.id} aria-labelledby={`conso-${pipeline.id}`}>
                <h2 className="consumption__title" id={`conso-${pipeline.id}`}>
                  {pipeline.id}
                </h2>

                {hasMeasures(view) ? (
                  <dl className="consumption__lines">
                    {view.lines.map((line) => (
                      <div className="consumption__line" key={line.label}>
                        <dt>{line.label}</dt>
                        <dd className={line.value === null ? 'consumption__absent' : undefined} data-numeric="">
                          {line.value ?? 'Non mesuré'}
                          {line.unit !== null && <span className="consumption__unit"> {line.unit}</span>}
                        </dd>
                      </div>
                    ))}
                  </dl>
                ) : (
                  <p className="consumption__none">
                    Aucune mesure de consommation n’a encore été publiée pour cette liaison.
                  </p>
                )}

                <dl className="consumption__lines">
                  <div className="consumption__line"><dt>{cost.creditsLabel}</dt><dd>{cost.credits ?? cost.absent}</dd></div>
                  <div className="consumption__line"><dt>{cost.amountLabel}</dt><dd>{cost.amount}</dd></div>
                </dl>
                {cost.caveat && <p role="note">{cost.caveat}</p>}
                <p className="consumption__none">{cost.window}</p>
                <p className="consumption__none">{cost.scope}</p>
                {pipeline.costs?.amount != null && <p className="consumption__none">{cost.explanation}</p>}
                <section className="consumption__infrastructure" aria-label={infrastructure.title}>
                  <h3 className="consumption__title">{infrastructure.title}</h3>
                  <dl className="consumption__lines">
                    {infrastructure.lines.map(line => <div className="consumption__line" key={line.label}>
                      <dt>{line.label}</dt><dd className={line.value === 'Non mesuré' ? 'consumption__absent' : undefined}>{line.value}</dd>
                      <dd className="consumption__detail">{line.detail}</dd>
                    </div>)}
                  </dl>
                  <p className="consumption__none">{infrastructure.note}</p>
                </section>
                <div className="consumption__limits">
                  <p className="consumption__limits-title">Ce que Quadringent ne mesure pas</p>
                  <ul>
                    {view.unmeasured.map((item) => (
                      <li key={item}>{item}</li>
                    ))}
                  </ul>
                </div>
              </section>
            );
          })}
        </div>
      )}
    </div>
  );
}
