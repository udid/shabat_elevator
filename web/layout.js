const dialog = document.getElementById('about-dialog');
document.getElementById('about-button').addEventListener('click', () => dialog.showModal());
dialog.addEventListener('click', (event) => {
  if (event.target !== dialog) return;
  const bounds = dialog.getBoundingClientRect();
  if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) dialog.close();
});

const settings = document.querySelector('.settings');
const settingsButton = document.getElementById('settings-button');
const settingsPanel = document.getElementById('settings-panel');

function closeSettings(restoreFocus = false) {
  settingsPanel.hidden = true;
  settingsButton.setAttribute('aria-expanded', 'false');
  if (restoreFocus) settingsButton.focus();
}

settingsButton.addEventListener('click', () => {
  const opening = settingsPanel.hidden;
  settingsPanel.hidden = !opening;
  settingsButton.setAttribute('aria-expanded', String(opening));
});
document.addEventListener('click', (event) => {
  if (!settings.contains(event.target)) closeSettings();
});
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && !settingsPanel.hidden) {
    event.preventDefault();
    closeSettings(true);
  }
});
document.addEventListener('focusin', (event) => {
  if (!settings.contains(event.target)) closeSettings();
});
document.getElementById('demo-button').addEventListener('click', () => closeSettings(true));
