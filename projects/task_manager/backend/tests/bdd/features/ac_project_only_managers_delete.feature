# ac.project.only_managers_delete  (requirements: requirement.project_management; operations: operation.project.delete)
Feature: Only managers delete projects (negative case)

  Scenario: Only managers delete projects (negative case)
    Given the user is signed in
    When an employee tries to delete a project
    Then Only managers can delete projects (error PROJECT_DELETE_FORBIDDEN)
    And no data is changed
