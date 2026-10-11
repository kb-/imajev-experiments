"""Explanations and model activity remain separate in the bottom Details tabs."""
from app.config import Config
from app.core.session import SessionController
from app.games.tic_tac_toe.game import TicTacToe
from app.ui.window import Window
from test_session import Fake, wait_for


def window(qapp, tmp_path):
    controller = SessionController(
        TicTacToe(), Fake(), Config(directory=tmp_path, diagnostics=False))
    view = Window(controller)
    wait_for(qapp, lambda: controller.ready and not controller.busy)
    return controller, view


def test_startup_separates_explanation_from_activity(qapp, tmp_path):
    controller, view = window(qapp, tmp_path)
    text = view.explanation.toPlainText()
    activity = view.diag.toPlainText()
    assert view.diag_button.isChecked() and not view.details_tabs.isHidden()
    assert view.diag_button.text() == 'Details ▾'
    assert view.details_tabs.currentWidget() is view.explanation
    assert [view.details_tabs.tabText(i) for i in range(2)] == ['How it works', 'Activity log']
    assert text.startswith('HOW THIS GAME WORKS\nTic-tac-toe · Quoted · ACTIVE')
    assert 'RULES\nX and O alternate' in text
    assert 'Recognition: imajev-4b-nf4' in text
    assert 'Opponent: imajev-4b-nf4' in text
    assert 'HOW DECISIONS ARE HANDLED' in text
    assert 'QUESTIONS ASKED' not in text
    assert 'HOW THIS GAME WORKS' not in activity and 'RULES\n' not in activity
    assert 'Recognition: imajev-4b-nf4' in activity
    assert 'QUESTIONS ASKED (NEWEST FIRST)' in activity
    view.close()


def test_mode_change_explains_active_and_pending_selection(qapp, tmp_path):
    controller, view = window(qapp, tmp_path)
    view.details_tabs.setCurrentWidget(view.diag)
    view.diag_button.setChecked(False)
    view.prompt_selector.setCurrentIndex(view.prompt_selector.findData('legacy'))
    text = view.explanation.toPlainText()
    assert view.details_tabs.currentWidget() is view.explanation
    assert view.diag_button.isChecked()
    assert text.startswith('HOW THIS GAME WORKS\nTic-tac-toe · Original · ACTIVE')
    assert 'opening advice' in text
    assert controller.config.prompt_variant == 'legacy'
    controller.state = controller.game.apply_action(controller.state, 'place_A1')
    controller._publish()
    view.prompt_selector.setCurrentIndex(view.prompt_selector.findData('quoted'))
    text = view.explanation.toPlainText()
    assert 'Tic-tac-toe · Quoted · PREVIEW — applies on New game' in text
    assert 'currently playing Original' in text
    assert controller.config.prompt_variant == 'legacy'
    assert 'PREVIEW' not in view.diag.toPlainText()
    view.close()


def test_game_change_preview_and_new_game_explanation(qapp, tmp_path):
    controller, view = window(qapp, tmp_path)
    controller.state = controller.game.apply_action(controller.state, 'place_A1')
    controller._publish()
    view.selector.setCurrentIndex(view.selector.findData('boku'))
    text = view.explanation.toPlainText()
    assert view.details_tabs.currentWidget() is view.explanation
    assert 'Boku · Quoted · PREVIEW — applies on New game' in text
    assert 'Five or more adjacent stones' in text
    assert 'bounded search, not a proof of perfect play' in text
    assert controller.game.id == 'tic_tac_toe'
    view.details_tabs.setCurrentWidget(view.diag)
    view.new_game()
    assert controller.game.id == 'boku'
    assert 'Boku · Quoted · ACTIVE' in view.explanation.toPlainText()
    assert view.details_tabs.currentWidget() is view.explanation
    view.close()


def test_tabs_preserve_independent_scroll_and_selection_during_live_updates(qapp, tmp_path):
    controller, view = window(qapp, tmp_path)
    view.resize(880, 690)
    view.show()
    qapp.processEvents()
    explanation = view.explanation.toPlainText()
    explanation_bar = view.explanation.verticalScrollBar()
    assert explanation_bar.maximum() > 0
    explanation_bar.setValue(explanation_bar.maximum() // 2)
    explanation_position = explanation_bar.value()
    view.details_tabs.setCurrentWidget(view.diag)
    controller.diagnostics = '\n'.join(f'Activity {i}' for i in range(100))
    controller._publish()
    qapp.processEvents()
    activity_bar = view.diag.verticalScrollBar()
    assert activity_bar.maximum() > 0
    activity_bar.setValue(activity_bar.maximum() // 3)
    activity_position = activity_bar.value()
    controller.diagnostics += '\nNew activity'
    controller._publish()
    assert view.details_tabs.currentWidget() is view.diag
    assert activity_bar.value() == activity_position
    assert explanation_bar.value() == explanation_position
    assert view.explanation.toPlainText() == explanation
    view.details_tabs.setCurrentWidget(view.explanation)
    controller.diagnostics += '\nMore activity'
    controller._publish()
    assert view.details_tabs.currentWidget() is view.explanation
    assert explanation_bar.value() == explanation_position
    view.details_tabs.setCurrentWidget(view.diag)
    assert activity_bar.value() == activity_position
    view.prompt_selector.setCurrentIndex(view.prompt_selector.findData('legacy'))
    assert view.details_tabs.currentWidget() is view.explanation
    assert explanation_bar.value() == 0
    assert activity_bar.value() == activity_position
    view.close()
