import os
import re
import logging
from datetime import datetime
from github import Github
from vcsp_interface import PR, Commit, PRFile, VCSPInterface
from github import Github, GithubException
from collections import defaultdict

logger = logging.getLogger(__name__)


def _parse_diff_per_file(diff_text):
    """Parse diff text into PRFile objects with changed lines."""
    try:
        files = []
        current_file = None
        current_diff = []
        changed_lines = set()
        line_num_new = None

        for line in diff_text.splitlines(keepends=False):
            if line.startswith('diff --git') or line.startswith('---') and '/dev/null' in line:
                if current_file and current_diff:
                    files.append(PRFile(current_file, '\n'.join(current_diff), changed_lines))
                current_diff = [line]
                changed_lines = set()
                line_num_new = None
                # Try to extract filename from diff header or next lines
                if line.startswith('diff --git'):
                    parts = line.split()
                    if len(parts) >= 3:
                        current_file = parts[2].replace('a/', '')
            elif line.startswith('+++'):
                # Extract filename from +++ line
                if '/dev/null' not in line:
                    current_file = line.replace('+++ b/', '').replace('+++ ', '').strip()
            elif current_file:
                current_diff.append(line)
                if line.startswith('@@'):
                    try:
                        match = re.search(r'\+(\d+)', line)
                        if match:
                            line_num_new = int(match.group(1)) - 1
                    except Exception as e:
                        logger.warning("Failed to parse hunk header: %s", line)
                elif line.startswith('+') and not line.startswith('+++'):
                    if line_num_new is not None:
                        line_num_new += 1
                        changed_lines.add(line_num_new)
                elif not line.startswith('-'):
                    if line_num_new is not None:
                        line_num_new += 1

        if current_file and current_diff:
            files.append(PRFile(current_file, '\n'.join(current_diff), changed_lines))

        return files

    except Exception as e:
        logger.error("Failed to parse diff text: %s", e)
        return []


class GithubVCSP(VCSPInterface):
    def __init__(self):
        token = os.getenv("GITHUB_TOKEN")
        if not token:
            raise ValueError("GITHUB_TOKEN environment variable is required")

        self.client = Github(token)

    def get_pull_request(self, repo_name: str, pr_number: int):
        try:
            github_pr = self.client.get_repo(repo_name).get_pull(pr_number)
            return PR(
                title=github_pr.title,
                body=github_pr.body,
                head_sha=github_pr.head.sha,
                state=github_pr.state
            )
        except GithubException as e:
            raise Exception(f"Failed to get GitHub PR {pr_number} in {repo_name}: {str(e)}")

    def get_last_ai_review_time(self, repo_name: str, pr_number: int):
        """Get the timestamp of the last AI review comment."""
        try:
            repo = self.client.get_repo(repo_name)
            pr = repo.get_pull(pr_number)
            last_time = None
            
            # Check review comments
            for comment in pr.get_review_comments():
                if "AI Comment:" in comment.body:
                    comment_time = comment.created_at
                    if not last_time or comment_time > last_time:
                        last_time = comment_time
            
            # Check issue comments
            for comment in pr.get_issue_comments():
                if "AI Comment:" in comment.body:
                    comment_time = comment.created_at
                    if not last_time or comment_time > last_time:
                        last_time = comment_time
            
            return last_time
        except GithubException as e:
            logger.error(f"Failed to get last AI review time: {str(e)}")
            return None

    def get_commits_after_time(self, repo_name: str, pr_number: int, since_time):
        """Get commits in PR after the given time."""
        try:
            repo = self.client.get_repo(repo_name)
            pr = repo.get_pull(pr_number)
            commits = []
            
            for commit in pr.get_commits():
                commit_time = commit.commit.author.date
                if commit_time > since_time:
                    commits.append({
                        "sha": commit.sha,
                        "date": commit_time.isoformat()
                    })
            
            return commits
        except GithubException as e:
            logger.error(f"Failed to get commits after time: {str(e)}")
            return []

    def get_commit_diff(self, repo_name: str, commit_hash: str):
        """Get diff for a specific commit."""
        try:
            repo = self.client.get_repo(repo_name)
            commit = repo.get_commit(commit_hash)
            
            files = []
            for file in commit.files:
                # Build diff-like text from file patch
                patch = file.patch or ""
                if patch:
                    # Extract changed lines from patch
                    changed_lines = set()
                    line_num_new = None
                    for line in patch.splitlines():
                        if line.startswith('@@'):
                            try:
                                match = re.search(r'\+(\d+)', line)
                                if match:
                                    line_num_new = int(match.group(1)) - 1
                            except Exception:
                                pass
                        elif line.startswith('+') and not line.startswith('+++'):
                            if line_num_new is not None:
                                line_num_new += 1
                                changed_lines.add(line_num_new)
                        elif not line.startswith('-'):
                            if line_num_new is not None:
                                line_num_new += 1
                    files.append(PRFile(file.filename, patch, changed_lines))
            
            return files
        except GithubException as e:
            logger.error(f"Failed to get commit diff for {commit_hash}: {str(e)}")
            return []

    def get_pr_diff(self, repo_name: str, pr_number: int):
        """Get full PR diff."""
        try:
            pr = self.client.get_repo(repo_name).get_pull(pr_number)
            files = []
            for file in pr.get_files():
                # Extract changed lines from patch if available
                changed_lines = set()
                if file.patch:
                    line_num_new = None
                    for line in file.patch.splitlines():
                        if line.startswith('@@'):
                            try:
                                match = re.search(r'\+(\d+)', line)
                                if match:
                                    line_num_new = int(match.group(1)) - 1
                            except Exception:
                                pass
                        elif line.startswith('+') and not line.startswith('+++'):
                            if line_num_new is not None:
                                line_num_new += 1
                                changed_lines.add(line_num_new)
                        elif not line.startswith('-'):
                            if line_num_new is not None:
                                line_num_new += 1
                files.append(PRFile(file.filename, file.patch or "", changed_lines))
            return files
        except GithubException as e:
            logger.error(f"Failed to get PR diff: {str(e)}")
            return []

    def get_files_in_pr(self, repo_name: str, pr_number: int):
        """Get files in PR, but only new changes if there was a previous AI review."""
        try:
            last_review_time = self.get_last_ai_review_time(repo_name, pr_number)

            if last_review_time:
                commits = self.get_commits_after_time(repo_name, pr_number, last_review_time)
                if not commits:
                    logger.info("No new commits after last AI review.")
                    return []

                per_commit_diffs = []
                for commit in commits:
                    commit_diff = self.get_commit_diff(repo_name, commit["sha"])
                    per_commit_diffs.append(commit_diff)

                # Conflict detection - merge diffs for same files
                merged = defaultdict(lambda: {"diff": [], "lines": set()})
                conflict_files = set()

                for commit_diff in per_commit_diffs:
                    for pr_file in commit_diff:
                        if pr_file.filename in merged:
                            if merged[pr_file.filename]["lines"] & pr_file.lines:
                                conflict_files.add(pr_file.filename)
                        merged[pr_file.filename]["diff"].append(pr_file)
                        if pr_file.lines:
                            merged[pr_file.filename]["lines"].update(pr_file.lines)

                # Final output
                final_files = []
                full_pr_diff = None  # lazy load
                for filename, group in merged.items():
                    if filename in conflict_files:
                        # Use full PR diff for conflicting files
                        if full_pr_diff is None:
                            full_pr_diff = {f.filename: f for f in self.get_pr_diff(repo_name, pr_number)}
                        if filename in full_pr_diff:
                            final_files.append(full_pr_diff[filename])
                        else:
                            logger.warning("Conflict file %s not found in PR diff", filename)
                    else:
                        # Merge patches from multiple commits for same file
                        patches = [f.patch for f in group["diff"] if f.patch]
                        if patches:
                            # Combine patches (simple concatenation, could be improved)
                            combined_patch = "\n".join(patches)
                            final_files.append(PRFile(filename, combined_patch, group["lines"]))

                return final_files
            else:
                # No AI review, return full PR diff
                logger.info("No previous AI comment found, taking full PR diff.")
                return self.get_pr_diff(repo_name, pr_number)
        except GithubException as e:
            raise Exception(f"Failed to get files in GitHub PR {pr_number}: {str(e)}")

    def get_file_content(self, repo_name: str, file_path: str, ref: str = None) -> str:
        try:
            content = self.client.get_repo(repo_name).get_contents(file_path, ref=ref)
            if content.decoded_content is None:
                raise ValueError(f"File content is not decodable (possibly binary) for {file_path}")
            return content.decoded_content.decode('utf-8')
        except UnicodeDecodeError as e:
            raise ValueError(f"Failed to decode file content for {file_path} (possibly binary): {str(e)}")
        except GithubException as e:
            raise Exception(f"Failed to get file content for {file_path} in {repo_name}: {str(e)}")

    def create_issue_comment(self, repo_name: str, pr_number: int, comment: str):
        """Create a general comment on a pull request (issue comment)."""
        try:
            repo = self.client.get_repo(repo_name)
            pr = repo.get_pull(pr_number)
            pr.create_issue_comment(comment)
            return True
        except GithubException as e:
            raise Exception(f"Failed to create GitHub issue comment: {str(e)}")

    def create_review_comment(self, repo_name: str, commit: str, file_path: str, line: int, comment: str, side: str):
        try:
            repo = self.client.get_repo(repo_name)
            commit_obj = repo.get_commit(commit)
            prs = commit_obj.get_pulls()
            if not prs.totalCount:
                raise Exception(f"No pull request found for commit {commit} in {repo_name}")
            pr = prs[0]
            
            # For inline comments, we need to find the correct line number in the PR diff
            # The line number from LLM might not match the actual file due to diff context
            # Let's get the PR file to find the correct line
            actual_line = line
            try:
                # Get the PR files to find the file we're commenting on
                pr_files = pr.get_files()
                target_file = None
                for pf in pr_files:
                    if pf.filename == file_path:
                        target_file = pf
                        break
                
                if target_file and target_file.patch:
                    # Find the closest changed line in the diff to the requested line
                    # Parse the diff to find actual changed lines
                    changed_lines = set()
                    line_num_new = None
                    for diff_line in target_file.patch.splitlines():
                        if diff_line.startswith('@@'):
                            try:
                                match = re.search(r'\+(\d+)', diff_line)
                                if match:
                                    line_num_new = int(match.group(1)) - 1
                            except Exception:
                                pass
                        elif diff_line.startswith('+') and not diff_line.startswith('+++'):
                            if line_num_new is not None:
                                line_num_new += 1
                                changed_lines.add(line_num_new)
                        elif not diff_line.startswith('-'):
                            if line_num_new is not None:
                                line_num_new += 1
                    
                    # Find the closest changed line to the requested line
                    if changed_lines:
                        # If the requested line is a changed line, use it
                        if line in changed_lines:
                            actual_line = line
                        else:
                            # Find the closest changed line
                            closest = min(changed_lines, key=lambda x: abs(x - line))
                            actual_line = closest
                            logger.debug(f"Line {line} not found in diff, using closest changed line {actual_line}")
                else:
                    # If file not found in PR or no patch, try using the original line
                    logger.warning(f"File {file_path} not found in PR diff or has no changes, using line {line}")
            except Exception as e:
                logger.warning(f"Could not determine correct line number: {str(e)}, using line {line}")
            
            print(f"Posting comment on {file_path} at position {actual_line} in commit {commit}")
            # Use direct API call to specify side="RIGHT" to show new code instead of old
            # The create_review_comment method doesn't support side parameter directly
            url = f"/repos/{repo_name}/pulls/{pr.number}/comments"
            data = {
                "body": comment,
                "commit_id": commit,
                "path": file_path,
                "line": actual_line,
                "side": "RIGHT"  # RIGHT = new code (changed lines), LEFT = old code
            }
            # Use the requester from the Github client to make the API call
            requester = self.client._Github__requester
            requester.requestJsonAndCheck("POST", url, input=data)
            return True
        except GithubException as e:
            error_msg = str(e)
            # Check for 422 validation errors (line number issues)
            if "422" in error_msg or "Validation Failed" in error_msg or "could not be resolved" in error_msg:
                detailed_msg = (
                    f"Failed to create GitHub review comment: {error_msg}\n"
                    f"This error typically means the line number ({line}) doesn't exist in the file at the specified commit.\n"
                    f"The line number might be from the diff context and not match the actual file.\n"
                    f"Trying to use the closest changed line from the PR diff."
                )
                logger.error(detailed_msg)
                # Try creating a general comment on the PR instead
                try:
                    repo = self.client.get_repo(repo_name)
                    commit_obj = repo.get_commit(commit)
                    prs = commit_obj.get_pulls()
                    if prs.totalCount > 0:
                        pr = prs[0]
                        fallback_comment = f"**AI Comment on {file_path} (near line {line}):**\n\n{comment}"
                        self.create_issue_comment(repo_name, pr.number, fallback_comment)
                        logger.info(f"Created fallback issue comment instead of inline comment")
                        return True
                except Exception as fallback_error:
                    logger.warning(f"Failed to create fallback comment: {str(fallback_error)}")
                raise Exception(detailed_msg)
            # Check for 403 permission errors
            if "403" in error_msg or "Forbidden" in error_msg or "Resource not accessible" in error_msg:
                detailed_msg = (
                    f"Failed to create GitHub review comment: {error_msg}\n"
                    f"This error typically means the GITHUB_TOKEN doesn't have sufficient permissions.\n"
                    f"For GitHub Actions workflows, use the built-in GITHUB_TOKEN which has automatic permissions.\n"
                    f"For Personal Access Tokens, ensure the token has 'repo' scope (or 'public_repo' for public repositories).\n"
                    f"Token permissions can be checked/updated at: https://github.com/settings/tokens"
                )
                raise Exception(detailed_msg)
            raise Exception(f"Failed to create GitHub review comment: {error_msg}")

    def get_commit(self, repo_name: str, commit_sha: str):
        """Retrieve a commit by its SHA from a GitHub repository."""
        try:
            repo = self.client.get_repo(repo_name)
            commit = repo.get_commit(commit_sha)
            return Commit(
                sha=commit.sha,
                message=commit.commit.message,
                author=commit.commit.author.name,
                date=commit.commit.author.date.isoformat()
            )
        except GithubException as e:
            raise Exception(f"Failed to get GitHub commit {commit_sha} in {repo_name}: {str(e)}")
